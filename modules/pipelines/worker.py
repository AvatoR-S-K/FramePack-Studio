import os
import json
import time
import traceback
import einops
import numpy as np
import torch
import datetime
from PIL import Image
from PIL.PngImagePlugin import PngInfo
from diffusers_helper.utils import save_bcthw_as_mp4, generate_timestamp
from diffusers_helper.memory import cpu, gpu, move_model_to_device_with_memory_preservation, offload_model_from_device_for_memory_preservation, fake_diffusers_current_device, unload_complete_models, load_model_as_complete
from diffusers_helper.thread_utils import AsyncStream
from diffusers_helper.gradio.progress_bar import make_progress_bar_html
from diffusers_helper.hunyuan import vae_decode
from modules.video_queue import JobStatus
from modules.prompt_handler import parse_timestamped_prompt
from modules.generators import create_model_generator
from . import create_pipeline

@torch.no_grad()
def get_cached_or_encode_prompt(prompt, text_encoder, text_encoder_2, tokenizer, tokenizer_2, target_device, prompt_embedding_cache):
    """
    Retrieves prompt embeddings from cache or encodes them if not found.
    Stores encoded embeddings (on CPU) in the cache.
    Returns embeddings moved to the target_device.
    """
    from diffusers_helper.hunyuan import encode_prompt_conds, crop_or_pad_yield_mask
    
    if prompt in prompt_embedding_cache:
        print(f"Cache hit for prompt: {prompt[:60]}...")
        llama_vec_cpu, llama_mask_cpu, clip_l_pooler_cpu = prompt_embedding_cache[prompt]
        # Move cached embeddings (from CPU) to the target device
        llama_vec = llama_vec_cpu.to(target_device)
        llama_attention_mask = llama_mask_cpu.to(target_device) if llama_mask_cpu is not None else None
        clip_l_pooler = clip_l_pooler_cpu.to(target_device)
        return llama_vec, llama_attention_mask, clip_l_pooler
    else:
        print(f"Cache miss for prompt: {prompt[:60]}...")
        llama_vec, clip_l_pooler = encode_prompt_conds(
            prompt, text_encoder, text_encoder_2, tokenizer, tokenizer_2
        )
        llama_vec, llama_attention_mask = crop_or_pad_yield_mask(llama_vec, length=512)
        # Store CPU copies in cache
        prompt_embedding_cache[prompt] = (llama_vec.cpu(), llama_attention_mask.cpu() if llama_attention_mask is not None else None, clip_l_pooler.cpu())
        # Return embeddings already on the target device (as encode_prompt_conds uses the model's device)
        return llama_vec, llama_attention_mask, clip_l_pooler

@torch.no_grad()
def worker(
    model_type,
    input_image,
    end_frame_image,     # The end frame image (numpy array or None)
    end_frame_strength,  # Influence of the end frame
    prompt_text, 
    n_prompt, 
    seed, 
    total_second_length, 
    latent_window_size,
    steps, 
    cfg, 
    gs, 
    rs, 
    use_teacache, 
    teacache_num_steps, 
    teacache_rel_l1_thresh,
    blend_sections, 
    latent_type,
    selected_loras,
    has_input_image,
    lora_values=None, 
    job_stream=None,
    output_dir=None,
    metadata_dir=None,
    input_files_dir=None,  # Add input_files_dir parameter
    input_image_path=None,  # Add input_image_path parameter
    end_frame_image_path=None,  # Add end_frame_image_path parameter
    resolutionW=640,  # Add resolution parameter with default value
    resolutionH=640,
    lora_loaded_names=[],
    input_video=None     # Add input_video parameter with default value of None
):
    """
    Worker function for video generation using the pipeline architecture.
    """
    # Import globals from the main module
    from __main__ import high_vram, current_generator, args, text_encoder, text_encoder_2, tokenizer, tokenizer_2, vae, image_encoder, feature_extractor, prompt_embedding_cache, settings, stream
    
    # Ensure any existing LoRAs are unloaded from the current generator
    if current_generator is not None:
        print("Unloading any existing LoRAs before starting new job")
        current_generator.unload_loras()
        import gc
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    
    stream_to_use = job_stream if job_stream is not None else stream

    total_latent_sections = (total_second_length * 30) / (latent_window_size * 4)
    total_latent_sections = int(max(round(total_latent_sections), 1))

    # --- Total progress tracking ---
    total_steps = total_latent_sections * steps  # Total diffusion steps over all segments
    step_durations = []  # Rolling history of recent step durations for ETA
    last_step_time = time.time()

    # Parse the timestamped prompt with boundary snapping and reversing
    # prompt_text should now be the original string from the job queue
    prompt_sections = parse_timestamped_prompt(prompt_text, total_second_length, latent_window_size, model_type)
    job_id = generate_timestamp()

    # Initialize progress data with a clear starting message
    initial_progress_data = {
        'preview': None,
        'desc': 'Starting job...',
        'html': make_progress_bar_html(0, 'Starting job...')
    }
    
    # Store initial progress data in the job object if using a job stream
    if job_stream is not None:
        try:
            from __main__ import job_queue
            job = job_queue.get_job(job_id)
            if job:
                job.progress_data = initial_progress_data
        except Exception as e:
            print(f"Error storing initial progress data: {e}")
    
    # Push initial progress update to both streams
    stream_to_use.output_queue.push(('progress', (None, 'Starting job...', make_progress_bar_html(0, 'Starting job...'))))
    
    # Also push to the main stream if using a job-specific stream
    from __main__ import stream as main_stream
    if job_stream is not None and stream_to_use != main_stream:
        print(f"Pushing initial progress update to main stream for job {job_id}")
        main_stream.output_queue.push(('progress', (None, 'Starting job...', make_progress_bar_html(0, 'Starting job...'))))

    try:
        # Create a settings dictionary for the pipeline
        pipeline_settings = {
            "output_dir": output_dir,
            "metadata_dir": metadata_dir,
            "input_files_dir": input_files_dir,
            "save_metadata": settings.get("save_metadata", True),
            "gpu_memory_preservation": settings.get("gpu_memory_preservation", 6),
            "mp4_crf": settings.get("mp4_crf", 16),
            "clean_up_videos": settings.get("clean_up_videos", True),
            "cleanup_temp_folder": settings.get("cleanup_temp_folder", True),
            "gradio_temp_dir": settings.get("gradio_temp_dir", "./gradio_temp"),
            "high_vram": high_vram
        }
        
        # Create the appropriate pipeline for the model type
        pipeline = create_pipeline(model_type, pipeline_settings)
        
        # Create job parameters dictionary
        job_params = {
            'model_type': model_type,
            'input_image': input_image,
            'end_frame_image': end_frame_image,
            'end_frame_strength': end_frame_strength,
            'prompt_text': prompt_text,
            'n_prompt': n_prompt,
            'seed': seed,
            'total_second_length': total_second_length,
            'latent_window_size': latent_window_size,
            'steps': steps,
            'cfg': cfg,
            'gs': gs,
            'rs': rs,
            'blend_sections': blend_sections,
            'latent_type': latent_type,
            'use_teacache': use_teacache,
            'teacache_num_steps': teacache_num_steps,
            'teacache_rel_l1_thresh': teacache_rel_l1_thresh,
            'selected_loras': selected_loras,
            'has_input_image': has_input_image,
            'lora_values': lora_values,
            'resolutionW': resolutionW,
            'resolutionH': resolutionH,
            'lora_loaded_names': lora_loaded_names,
            'input_image_path': input_image_path,
            'end_frame_image_path': end_frame_image_path
        }
        
        # Validate parameters
        is_valid, error_message = pipeline.validate_parameters(job_params)
        if not is_valid:
            raise ValueError(f"Invalid parameters: {error_message}")
        
        # Prepare parameters
        job_params = pipeline.prepare_parameters(job_params)
        
        if not high_vram:
            # Unload everything *except* the potentially active transformer
            unload_complete_models(text_encoder, text_encoder_2, image_encoder, vae)
            if current_generator is not None and current_generator.transformer is not None:
                offload_model_from_device_for_memory_preservation(current_generator.transformer, target_device=gpu, preserved_memory_gb=8)

        # --- Model Loading / Switching ---
        print(f"Worker starting for model type: {model_type}")
        
        # Create the appropriate model generator
        new_generator = create_model_generator(
            model_type,
            text_encoder=text_encoder,
            text_encoder_2=text_encoder_2,
            tokenizer=tokenizer,
            tokenizer_2=tokenizer_2,
            vae=vae,
            image_encoder=image_encoder,
            feature_extractor=feature_extractor,
            high_vram=high_vram,
            prompt_embedding_cache=prompt_embedding_cache,
            offline=args.offline,
            settings=settings
        )
        
        # Update the global generator
        current_generator = new_generator
        
        # Load the transformer model
        current_generator.load_model()
        
        # Ensure the model has no LoRAs loaded
        print(f"Ensuring {model_type} model has no LoRAs loaded")
        current_generator.unload_loras()

        # Preprocess inputs
        stream_to_use.output_queue.push(('progress', (None, '', make_progress_bar_html(0, 'Preprocessing inputs...'))))
        processed_inputs = pipeline.preprocess_inputs(job_params)
        
        # Update job_params with processed inputs
        job_params.update(processed_inputs)
        
        # Save the starting image directly to the output directory
        if settings.get("save_metadata") and job_params.get('input_image') is not None: # Check if metadata saving is enabled overall
            try:
                # Ensure output directory exists
                os.makedirs(output_dir, exist_ok=True)
                
                # Get the input image
                input_image_np = job_params.get('input_image')
                
                if isinstance(input_image_np, np.ndarray):
                    # Create PNG metadata
                    png_metadata = PngInfo()
                    png_metadata.add_text("prompt", job_params.get('prompt_text', ''))
                    png_metadata.add_text("seed", str(job_params.get('seed', 0)))
                    png_metadata.add_text("model_type", job_params.get('model_type', "Unknown"))
                    
                    # Convert image if needed
                    if input_image_np.dtype != np.uint8:
                        if input_image_np.max() <= 1.0 and input_image_np.min() >= -1.0 and input_image_np.dtype in [np.float32, np.float64]:
                            input_image_np = ((input_image_np + 1.0) / 2.0 * 255.0).clip(0, 255).astype(np.uint8)
                        elif input_image_np.max() <= 1.0 and input_image_np.min() >= 0.0 and input_image_np.dtype in [np.float32, np.float64]:
                            input_image_np = (input_image_np * 255.0).clip(0, 255).astype(np.uint8)
                        else:
                            input_image_np = input_image_np.clip(0, 255).astype(np.uint8)
                    
                    # Save the image
                    start_image_path = os.path.join(output_dir, f'{job_id}.png')
                    Image.fromarray(input_image_np).save(start_image_path, pnginfo=png_metadata)
            except Exception as e:
                print(f"Error saving starting image: {e}")
                traceback.print_exc()
        
        # Pre-encode all prompts
        stream_to_use.output_queue.push(('progress', (None, '', make_progress_bar_html(0, 'Text encoding all prompts...'))))

        if not high_vram:
            fake_diffusers_current_device(text_encoder, gpu)
            load_model_as_complete(text_encoder_2, target_device=gpu)

        # PROMPT BLENDING: Pre-encode all prompts and store in a list in order
        unique_prompts = []
        for section in prompt_sections:
            if section.prompt not in unique_prompts:
                unique_prompts.append(section.prompt)

        encoded_prompts = {}
        for prompt in unique_prompts:
            # Use the helper function for caching and encoding
            llama_vec, llama_attention_mask, clip_l_pooler = get_cached_or_encode_prompt(
                prompt, text_encoder, text_encoder_2, tokenizer, tokenizer_2, gpu, prompt_embedding_cache
            )
            encoded_prompts[prompt] = (llama_vec, llama_attention_mask, clip_l_pooler)

        # PROMPT BLENDING: Build a list of (start_section_idx, prompt) for each prompt
        prompt_change_indices = []
        last_prompt = None
        for idx, section in enumerate(prompt_sections):
            if section.prompt != last_prompt:
                prompt_change_indices.append((idx, section.prompt))
                last_prompt = section.prompt

        # Encode negative prompt
        if cfg == 1:
            llama_vec_n, llama_attention_mask_n, clip_l_pooler_n = (
                torch.zeros_like(encoded_prompts[prompt_sections[0].prompt][0]),
                torch.zeros_like(encoded_prompts[prompt_sections[0].prompt][1]),
                torch.zeros_like(encoded_prompts[prompt_sections[0].prompt][2])
            )
        else:
             # Use the helper function for caching and encoding negative prompt
            # Ensure n_prompt is a string
            n_prompt_str = str(n_prompt) if n_prompt is not None else ""
            llama_vec_n, llama_attention_mask_n, clip_l_pooler_n = get_cached_or_encode_prompt(
                n_prompt_str, text_encoder, text_encoder_2, tokenizer, tokenizer_2, gpu, prompt_embedding_cache
            )

        # Process input image or video based on model type
        if model_type == "Video":
            stream_to_use.output_queue.push(('progress', (None, '', make_progress_bar_html(0, 'Video processing ...'))))
            
            # Encode the video using the VideoModelGenerator
            start_latent, input_image_np, video_latents, fps, height, width, input_video_pixels = current_generator.video_encode(
                video_path=job_params['input_image'],  # For Video model, input_image contains the video path
                resolution=job_params['resolutionW'],
                no_resize=False,
                vae_batch_size=16,
                device=gpu,
                input_files_dir=job_params['input_files_dir']
            )
            
            # CLIP Vision encoding for the first frame
            stream_to_use.output_queue.push(('progress', (None, '', make_progress_bar_html(0, 'CLIP Vision encoding ...'))))
            
            if not high_vram:
                load_model_as_complete(image_encoder, target_device=gpu)
                
            from diffusers_helper.clip_vision import hf_clip_vision_encode
            image_encoder_output = hf_clip_vision_encode(input_image_np, feature_extractor, image_encoder)
            image_encoder_last_hidden_state = image_encoder_output.last_hidden_state
            
            # Store the input video pixels and latents for later use
            input_video_pixels = input_video_pixels.cpu()
            video_latents = video_latents.cpu()
            
            # Store the full video latents in the generator instance for preparing clean latents
            if hasattr(current_generator, 'set_full_video_latents'):
                current_generator.set_full_video_latents(video_latents.clone())
                print(f"Stored full input video latents in VideoModelGenerator. Shape: {video_latents.shape}")
            
            # For Video model, history_latents is initialized with the video_latents
            history_latents = video_latents
            
            # Store the last frame of the video latents as start_latent for the model
            start_latent = video_latents[:, :, -1:].cpu()
            print(f"Using last frame of input video as start_latent. Shape: {start_latent.shape}")
            print(f"Placed last frame of video at position 0 in history_latents")
            
            print(f"Initialized history_latents with video context. Shape: {history_latents.shape}")
            
            # Initialize total_generated_latent_frames for Video model
            # For Video model, we start with 0 since we'll be adding to the end of the video
            total_generated_latent_frames = 0
            
            # Store the number of frames in the input video for later use
            input_video_frame_count = video_latents.shape[2]
        else:
            # Regular image processing
            input_image_np = job_params['input_image']
            height = job_params['height']
            width = job_params['width']
            
            input_image_pt = torch.from_numpy(input_image_np).float() / 127.5 - 1
            input_image_pt = input_image_pt.permute(2, 0, 1)[None, :, None]

            # VAE encoding
            stream_to_use.output_queue.push(('progress', (None, '', make_progress_bar_html(0, 'VAE encoding ...'))))

            if not high_vram:
                load_model_as_complete(vae, target_device=gpu)

            from diffusers_helper.hunyuan import vae_encode
            start_latent = vae_encode(input_image_pt, vae)

            # CLIP Vision
            stream_to_use.output_queue.push(('progress', (None, '', make_progress_bar_html(0, 'CLIP Vision encoding ...'))))

            if not high_vram:
                load_model_as_complete(image_encoder, target_device=gpu)

            from diffusers_helper.clip_vision import hf_clip_vision_encode
            image_encoder_output = hf_clip_vision_encode(input_image_np, feature_extractor, image_encoder)
            image_encoder_last_hidden_state = image_encoder_output.last_hidden_state

        # VAE encode end_frame_image if provided
        end_frame_latent = None
        if (model_type == "Original with Endframe" or model_type == "F1 with Endframe") and job_params.get('end_frame_image') is not None:
            print(f"Processing end frame for {model_type} model...")
            end_frame_image = job_params['end_frame_image']
            
            if not isinstance(end_frame_image, np.ndarray):
                print(f"Warning: end_frame_image is not a numpy array (type: {type(end_frame_image)}). Attempting conversion or skipping.")
                try:
                    end_frame_image = np.array(end_frame_image)
                except Exception as e_conv:
                    print(f"Could not convert end_frame_image to numpy array: {e_conv}. Skipping end frame.")
                    end_frame_image = None
            
            if end_frame_image is not None:
                # Use the main job's target width/height (bucket dimensions) for the end frame
                end_frame_np = job_params['end_frame_image']
                
                if settings.get("save_metadata"):
                    Image.fromarray(end_frame_np).save(os.path.join(metadata_dir, f'{job_id}_end_frame_processed.png'))
                
                end_frame_pt = torch.from_numpy(end_frame_np).float() / 127.5 - 1
                end_frame_pt = end_frame_pt.permute(2, 0, 1)[None, :, None] # VAE expects [B, C, F, H, W]
                
                if not high_vram: load_model_as_complete(vae, target_device=gpu) # Ensure VAE is loaded
                from diffusers_helper.hunyuan import vae_encode
                end_frame_latent = vae_encode(end_frame_pt, vae)
                print("End frame VAE encoded.")
        
        if not high_vram: # Offload VAE and image_encoder if they were loaded
            offload_model_from_device_for_memory_preservation(vae, target_device=gpu, preserved_memory_gb=settings.get("gpu_memory_preservation"))
            offload_model_from_device_for_memory_preservation(image_encoder, target_device=gpu, preserved_memory_gb=settings.get("gpu_memory_preservation"))
        
        # Dtype
        for prompt_key in encoded_prompts:
            llama_vec, llama_attention_mask, clip_l_pooler = encoded_prompts[prompt_key]
            llama_vec = llama_vec.to(current_generator.transformer.dtype)
            clip_l_pooler = clip_l_pooler.to(current_generator.transformer.dtype)
            encoded_prompts[prompt_key] = (llama_vec, llama_attention_mask, clip_l_pooler)

        llama_vec_n = llama_vec_n.to(current_generator.transformer.dtype)
        clip_l_pooler_n = clip_l_pooler_n.to(current_generator.transformer.dtype)
        image_encoder_last_hidden_state = image_encoder_last_hidden_state.to(current_generator.transformer.dtype)

        # Sampling
        stream_to_use.output_queue.push(('progress', (None, '', make_progress_bar_html(0, 'Start sampling ...'))))

        rnd = torch.Generator("cpu").manual_seed(seed)
        num_frames = latent_window_size * 4 - 3

        # Initialize history latents and total_generated_latent_frames based on model type
        total_generated_latent_frames = 0  # Default initialization for all model types
        
        if model_type != "Video":  # Skip for Video model as we already initialized it
            history_latents = current_generator.prepare_history_latents(height, width)
            
            # For F1 model, initialize with start latent
            if model_type == "F1" or model_type == "F1 with Endframe":
                history_latents = current_generator.initialize_with_start_latent(history_latents, start_latent)
                total_generated_latent_frames = 1  # Start with 1 for F1 model since it includes the first frame
            elif model_type == "Original" or model_type == "Original with Endframe":
                total_generated_latent_frames = 0

        history_pixels = None
        
        # Get latent paddings from the generator
        latent_paddings = current_generator.get_latent_paddings(total_latent_sections)

        # PROMPT BLENDING: Track section index
        section_idx = 0

        # Load LoRAs if selected
        if selected_loras:
            lora_folder_from_settings = settings.get("lora_dir")
            current_generator.load_loras(selected_loras, lora_folder_from_settings, lora_loaded_names, lora_values)

            # --- Callback for progress ---
        def callback(d):
            nonlocal last_step_time, step_durations
            
            # Check for cancellation signal
            if stream_to_use.input_queue.top() == 'end':
                print("Cancellation signal detected in callback")
                return 'cancel'  # Return a signal that will be checked in the sampler
                
            now_time = time.time()
            # Record duration between diffusion steps (skip first where duration may include setup)
            if last_step_time is not None:
                step_delta = now_time - last_step_time
                if step_delta > 0:
                    step_durations.append(step_delta)
                    if len(step_durations) > 30:  # Keep only recent 30 steps
                        step_durations.pop(0)
            last_step_time = now_time
            avg_step = sum(step_durations) / len(step_durations) if step_durations else 0.0

            preview = d['denoised']
            from diffusers_helper.hunyuan import vae_decode_fake
            preview = vae_decode_fake(preview)
            preview = (preview * 255.0).detach().cpu().numpy().clip(0, 255).astype(np.uint8)
            preview = einops.rearrange(preview, 'b c t h w -> (b h) (t w) c')

            # --- Progress & ETA logic ---
            # Current segment progress
            current_step = d['i'] + 1
            percentage = int(100.0 * current_step / steps)

            # Total progress
            total_steps_done = section_idx * steps + current_step
            total_percentage = int(100.0 * total_steps_done / total_steps)

            # ETA calculations
            def fmt_eta(sec):
                try:
                    return str(datetime.timedelta(seconds=int(sec)))
                except Exception:
                    return "--:--"

            segment_eta = (steps - current_step) * avg_step if avg_step else 0
            total_eta = (total_steps - total_steps_done) * avg_step if avg_step else 0

            segment_hint = f'Sampling {current_step}/{steps}  ETA {fmt_eta(segment_eta)}'
            total_hint = f'Total {total_steps_done}/{total_steps}  ETA {fmt_eta(total_eta)}'

            # For Video model, add the input video frame count when calculating current position
            if model_type == "Video":
                # Calculate the time position including the input video frames
                input_video_time = input_video_frame_count * 4 / 30  # Convert latent frames to time
                current_pos = input_video_time + (total_generated_latent_frames * 4 - 3) / 30
                # Original position is the remaining time to generate
                original_pos = total_second_length - (total_generated_latent_frames * 4 - 3) / 30
            else:
                # For other models, calculate as before
                current_pos = (total_generated_latent_frames * 4 - 3) / 30
                original_pos = total_second_length - current_pos
            
            # Ensure positions are not negative
            if current_pos < 0: current_pos = 0
            if original_pos < 0: original_pos = 0

            hint = segment_hint  # deprecated variable kept to minimise other code changes
            desc = current_generator.format_position_description(
                total_generated_latent_frames, 
                current_pos, 
                original_pos, 
                current_prompt
            )

            # Create progress data dictionary
            progress_data = {
                'preview': preview,
                'desc': desc,
                'html': make_progress_bar_html(percentage, segment_hint) + make_progress_bar_html(total_percentage, total_hint)
            }
            
            # Store progress data in the job object
            if job_stream is not None:
                try:
                    from __main__ import job_queue
                    job = job_queue.get_job(job_id)
                    if job:
                        job.progress_data = progress_data
                except Exception as e:
                    print(f"Error updating job progress data: {e}")
                    
            # Always push to the job-specific stream
            stream_to_use.output_queue.push(('progress', (preview, desc, make_progress_bar_html(percentage, segment_hint) + make_progress_bar_html(total_percentage, total_hint))))
            
            # Always push to the main stream to ensure the UI is updated
            # This is especially important for resumed jobs
            from __main__ import stream as main_stream
            if main_stream and stream_to_use != main_stream:
                main_stream.output_queue.push(('progress', (preview, desc, make_progress_bar_html(percentage, segment_hint) + make_progress_bar_html(total_percentage, total_hint))))

        # --- Main generation loop ---
        # `i_section_loop` will be our loop counter for applying end_frame_latent
        for i_section_loop, latent_padding in enumerate(latent_paddings): # Existing loop structure
            is_last_section = latent_padding == 0
            latent_padding_size = latent_padding * latent_window_size

            if stream_to_use.input_queue.top() == 'end':
                stream_to_use.output_queue.push(('end', None))
                return

            # Calculate the current time position
            if model_type == "Video":
                # For Video model, add the input video time to the current position
                input_video_time = input_video_frame_count * 4 / 30  # Convert latent frames to time
                current_time_position = (total_generated_latent_frames * 4 - 3) / 30  # in seconds
                if current_time_position < 0:
                    current_time_position = 0.01
            else:
                # For other models, calculate as before
                current_time_position = (total_generated_latent_frames * 4 - 3) / 30  # in seconds
                if current_time_position < 0:
                    current_time_position = 0.01

            # Find the appropriate prompt for this section
            current_prompt = prompt_sections[0].prompt  # Default to first prompt
            for section in prompt_sections:
                if section.start_time <= current_time_position and (section.end_time is None or current_time_position < section.end_time):
                    current_prompt = section.prompt
                    break

            # PROMPT BLENDING: Find if we're in a blend window
            blend_alpha = None
            prev_prompt = current_prompt
            next_prompt = current_prompt

            # Only try to blend if blend_sections > 0 and we have prompt change indices and multiple sections
            if blend_sections > 0 and prompt_change_indices and len(prompt_sections) > 1:
                for i, (change_idx, prompt) in enumerate(prompt_change_indices):
                    if section_idx < change_idx:
                        prev_prompt = prompt_change_indices[i - 1][1] if i > 0 else prompt
                        next_prompt = prompt
                        blend_start = change_idx
                        blend_end = change_idx + blend_sections
                        if section_idx >= change_idx and section_idx < blend_end:
                            blend_alpha = (section_idx - change_idx + 1) / blend_sections
                        break
                    elif section_idx == change_idx:
                        # At the exact change, start blending
                        if i > 0:
                            prev_prompt = prompt_change_indices[i - 1][1]
                            next_prompt = prompt
                            blend_alpha = 1.0 / blend_sections
                        else:
                            prev_prompt = prompt
                            next_prompt = prompt
                            blend_alpha = None
                        break
                else:
                    # After last change, no blending
                    prev_prompt = current_prompt
                    next_prompt = current_prompt
                    blend_alpha = None

            # Get the encoded prompt for this section
            if blend_alpha is not None and prev_prompt != next_prompt:
                # Blend embeddings
                prev_llama_vec, prev_llama_attention_mask, prev_clip_l_pooler = encoded_prompts[prev_prompt]
                next_llama_vec, next_llama_attention_mask, next_clip_l_pooler = encoded_prompts[next_prompt]
                llama_vec = (1 - blend_alpha) * prev_llama_vec + blend_alpha * next_llama_vec
                llama_attention_mask = prev_llama_attention_mask  # usually same
                clip_l_pooler = (1 - blend_alpha) * prev_clip_l_pooler + blend_alpha * next_clip_l_pooler
                print(f"Blending prompts: '{prev_prompt[:30]}...' -> '{next_prompt[:30]}...', alpha={blend_alpha:.2f}")
            else:
                llama_vec, llama_attention_mask, clip_l_pooler = encoded_prompts[current_prompt]

            original_time_position = total_second_length - current_time_position
            if original_time_position < 0:
                original_time_position = 0

            print(f'latent_padding_size = {latent_padding_size}, is_last_section = {is_last_section}, '
                  f'time position: {current_time_position:.2f}s (original: {original_time_position:.2f}s), '
                  f'using prompt: {current_prompt[:60]}...')

            # Apply end_frame_latent to history_latents for models with Endframe support
            if (model_type == "Original with Endframe" or model_type == "F1 with Endframe") and i_section_loop == 0 and end_frame_latent is not None:
                print(f"Applying end_frame_latent to history_latents with strength: {end_frame_strength}")
                actual_end_frame_latent_for_history = end_frame_latent.clone()
                if end_frame_strength != 1.0: # Only multiply if not full strength
                    actual_end_frame_latent_for_history = actual_end_frame_latent_for_history * end_frame_strength
                
                # Ensure history_latents is on the correct device (usually CPU for this kind of modification if it's init'd there)
                # and that the assigned tensor matches its dtype.
                # The `current_generator.prepare_history_latents` initializes it on CPU with float32.
                if history_latents.shape[2] >= 1: # Check if the 'Depth_slots' dimension is sufficient
                    if model_type == "Original with Endframe":
                        # For Original model, apply to the beginning (position 0)
                        history_latents[:, :, 0:1, :, :] = actual_end_frame_latent_for_history.to(
                            device=history_latents.device, # Assign to history_latents' current device
                            dtype=history_latents.dtype    # Match history_latents' dtype
                        )
                    elif model_type == "F1 with Endframe":
                        # For F1 model, apply to the end (last position)
                        history_latents[:, :, -1:, :, :] = actual_end_frame_latent_for_history.to(
                            device=history_latents.device, # Assign to history_latents' current device
                            dtype=history_latents.dtype    # Match history_latents' dtype
                        )
                    print(f"End frame latent applied to history for {model_type} model.")
                else:
                    print("Warning: history_latents not shaped as expected for end_frame application.")
            
            
            # Check if the generator has the combined prepare_clean_latents_and_indices method
            if hasattr(current_generator, 'prepare_clean_latents_and_indices'):
                clean_latent_indices, latent_indices, clean_latent_2x_indices, clean_latent_4x_indices, clean_latents, clean_latents_2x, clean_latents_4x = \
                current_generator.prepare_clean_latents_and_indices(latent_paddings, latent_padding, latent_padding_size, latent_window_size, video_latents, history_latents)
            else:
                # Prepare indices using the generator
                clean_latent_indices, latent_indices, clean_latent_2x_indices, clean_latent_4x_indices = current_generator.prepare_indices(latent_padding_size, latent_window_size)

                # Prepare clean latents using the generator
                clean_latents, clean_latents_2x, clean_latents_4x = current_generator.prepare_clean_latents(start_latent, history_latents)
            
            # Print debug info
            print(f"{model_type} model section {section_idx+1}/{total_latent_sections}, latent_padding={latent_padding}")

            if not high_vram:
                # Unload VAE etc. before loading transformer
                unload_complete_models(vae, text_encoder, text_encoder_2, image_encoder)
                move_model_to_device_with_memory_preservation(current_generator.transformer, target_device=gpu, preserved_memory_gb=settings.get("gpu_memory_preservation"))
                if selected_loras:
                    current_generator.move_lora_adapters_to_device(gpu)

            if use_teacache:
                current_generator.transformer.initialize_teacache(enable_teacache=True, num_steps=teacache_num_steps, rel_l1_thresh=teacache_rel_l1_thresh)
            else:
                current_generator.transformer.initialize_teacache(enable_teacache=False)

            from diffusers_helper.pipelines.k_diffusion_hunyuan import sample_hunyuan
            generated_latents = sample_hunyuan(
                transformer=current_generator.transformer,
                width=width,
                height=height,
                frames=num_frames,
                real_guidance_scale=cfg,
                distilled_guidance_scale=gs,
                guidance_rescale=rs,
                num_inference_steps=steps,
                generator=rnd,
                prompt_embeds=llama_vec,
                prompt_embeds_mask=llama_attention_mask,
                prompt_poolers=clip_l_pooler,
                negative_prompt_embeds=llama_vec_n,
                negative_prompt_embeds_mask=llama_attention_mask_n,
                negative_prompt_poolers=clip_l_pooler_n,
                device=gpu,
                dtype=torch.bfloat16,
                image_embeddings=image_encoder_last_hidden_state,
                latent_indices=latent_indices,
                clean_latents=clean_latents,
                clean_latent_indices=clean_latent_indices,
                clean_latents_2x=clean_latents_2x,
                clean_latent_2x_indices=clean_latent_2x_indices,
                clean_latents_4x=clean_latents_4x,
                clean_latent_4x_indices=clean_latent_4x_indices,
                callback=callback,
            )

            total_generated_latent_frames += int(generated_latents.shape[2])
            # Update history latents using the generator
            history_latents = current_generator.update_history_latents(history_latents, generated_latents)

            if not high_vram:
                if selected_loras:
                    current_generator.move_lora_adapters_to_device(cpu)
                offload_model_from_device_for_memory_preservation(current_generator.transformer, target_device=gpu, preserved_memory_gb=8)
                load_model_as_complete(vae, target_device=gpu)

            # Get real history latents using the generator
            real_history_latents = current_generator.get_real_history_latents(history_latents, total_generated_latent_frames)

            if history_pixels is None:
                history_pixels = vae_decode(real_history_latents, vae).cpu()
            else:
                section_latent_frames = current_generator.get_section_latent_frames(latent_window_size, is_last_section)
                overlapped_frames = latent_window_size * 4 - 3

                # Get current pixels using the generator
                current_pixels = current_generator.get_current_pixels(real_history_latents, section_latent_frames, vae)
                
                # Update history pixels using the generator
                history_pixels = current_generator.update_history_pixels(history_pixels, current_pixels, overlapped_frames)
                
                print(f"{model_type} model section {section_idx+1}/{total_latent_sections}, history_pixels shape: {history_pixels.shape}")

            if not high_vram:
                unload_complete_models()

            output_filename = os.path.join(output_dir, f'{job_id}_{total_generated_latent_frames}.mp4')
            save_bcthw_as_mp4(history_pixels, output_filename, fps=30, crf=settings.get("mp4_crf"))
            print(f'Decoded. Current latent shape {real_history_latents.shape}; pixel shape {history_pixels.shape}')
            stream_to_use.output_queue.push(('file', output_filename))

            if is_last_section:
                break

            section_idx += 1  # PROMPT BLENDING: increment section index

        # For Video model, concatenate the input video with the generated content
        if model_type == "Video":
            print("Concatenating input video with generated content...")
            # Since the generation happens in reverse order, we need to reverse the history_pixels
            # before concatenating with the input video
            print(f"Reversing generated content. Shape before: {history_pixels.shape}")
            # Reverse the frames along the time dimension (dim=2)
            reversed_history_pixels = torch.flip(history_pixels, dims=[2])
            print(f"Shape after reversal: {reversed_history_pixels.shape}")
            
            # Get the last frame of the input video and the first frame of the reversed generated video
            last_input_frame = input_video_pixels[:, :, -1:, :, :]
            first_gen_frame = reversed_history_pixels[:, :, 0:1, :, :]
            print(f"Last input frame shape: {last_input_frame.shape}")
            print(f"First generated frame shape: {first_gen_frame.shape}")
            
            # Calculate the difference between the frames
            frame_diff = first_gen_frame - last_input_frame
            print(f"Frame difference magnitude: {torch.abs(frame_diff).mean().item()}")
            
            # Blend the first few frames of the generated video to create a smoother transition
            blend_frames = 5  # Number of frames to blend
            if reversed_history_pixels.shape[2] > blend_frames:
                print(f"Blending first {blend_frames} frames for smoother transition")
                for i in range(blend_frames):
                    # Calculate blend factor (1.0 at frame 0, decreasing to 0.0)
                    blend_factor = 1.0 - (i / blend_frames)
                    # Apply correction with decreasing strength
                    reversed_history_pixels[:, :, i:i+1, :, :] = reversed_history_pixels[:, :, i:i+1, :, :] - frame_diff * blend_factor
            
            # Concatenate the input video pixels with the reversed history pixels
            # The input video should come first, followed by the generated content
            # This makes the video extend from where the input video ends
            combined_pixels = torch.cat([input_video_pixels, reversed_history_pixels], dim=2)
            
            # Create the final video with both input and generated content
            output_filename = os.path.join(output_dir, f'{job_id}_final_with_input.mp4')
            save_bcthw_as_mp4(combined_pixels, output_filename, fps=30, crf=settings.get("mp4_crf"))
            print(f'Final video with input: {output_filename}')
            stream_to_use.output_queue.push(('file', output_filename))

        # Create metadata for the job
        pipeline.create_metadata(job_params, job_id)

        # Handle the results
        result = pipeline.handle_results(job_params, output_filename)

        # Unload all LoRAs after generation completed
        if selected_loras:
            print("Unloading all LoRAs after generation completed")
            current_generator.unload_loras()
            import gc
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

    except Exception as e:
        traceback.print_exc()
        # Unload all LoRAs after error
        if current_generator is not None and selected_loras:
            print("Unloading all LoRAs after error")
            current_generator.unload_loras()
            import gc
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                
        stream_to_use.output_queue.push(('error', f"Error during generation: {traceback.format_exc()}"))
        if not high_vram:
            # Ensure all models including the potentially active transformer are unloaded on error
            unload_complete_models(
                text_encoder, text_encoder_2, image_encoder, vae, 
                current_generator.transformer if current_generator else None
            )

    if settings.get("clean_up_videos"):
        try:
            video_files = [
                f for f in os.listdir(output_dir)
                if f.startswith(f"{job_id}_") and f.endswith(".mp4")
            ]
            print(f"Video files found for cleanup: {video_files}")
            if video_files:
                def get_frame_count(filename):
                    try:
                        # Handles filenames like jobid_123.mp4
                        return int(filename.replace(f"{job_id}_", "").replace(".mp4", ""))
                    except Exception:
                        return -1
                video_files_sorted = sorted(video_files, key=get_frame_count)
                print(f"Sorted video files: {video_files_sorted}")
                final_video = video_files_sorted[-1]
                for vf in video_files_sorted[:-1]:
                    full_path = os.path.join(output_dir, vf)
                    try:
                        os.remove(full_path)
                        print(f"Deleted intermediate video: {full_path}")
                    except Exception as e:
                        print(f"Failed to delete {full_path}: {e}")
        except Exception as e:
            print(f"Error during video cleanup: {e}")
    
    # Clean up temp folder if enabled
    if settings.get("cleanup_temp_folder"):
        try:
            temp_dir = settings.get("gradio_temp_dir")
            if temp_dir and os.path.exists(temp_dir):
                print(f"Cleaning up temp folder: {temp_dir}")
                items = os.listdir(temp_dir)
                removed_count = 0
                for item in items:
                    item_path = os.path.join(temp_dir, item)
                    try:
                        if os.path.isfile(item_path) or os.path.islink(item_path):
                            os.remove(item_path)
                            removed_count += 1
                        elif os.path.isdir(item_path):
                            shutil.rmtree(item_path)
                            removed_count += 1
                    except Exception as e:
                        print(f"Error removing {item_path}: {e}")
                print(f"Cleaned up {removed_count} temporary files/folders.")
        except Exception as e:
            print(f"Error during temp folder cleanup: {e}")

    # Final verification of LoRA state
    if current_generator and current_generator.transformer:
        # Verify LoRA state
        has_loras = False
        if hasattr(current_generator.transformer, 'peft_config'):
            adapter_names = list(current_generator.transformer.peft_config.keys()) if current_generator.transformer.peft_config else []
            if adapter_names:
                has_loras = True
                print(f"Transformer has LoRAs: {', '.join(adapter_names)}")
            else:
                print(f"Transformer has no LoRAs in peft_config")
        else:
            print(f"Transformer has no peft_config attribute")
            
        # Check for any LoRA modules
        for name, module in current_generator.transformer.named_modules():
            if hasattr(module, 'lora_A') and module.lora_A:
                has_loras = True
            if hasattr(module, 'lora_B') and module.lora_B:
                has_loras = True
                
        if not has_loras:
            print(f"No LoRA components found in transformer")

    stream_to_use.output_queue.push(('end', None))
    return
