from torch.nn.parallel import DistributedDataParallel as DDP
import importlib
import argparse
import gc
import math
import os
import random
import time
import json
import toml
from multiprocessing import Value

from tqdm import tqdm
import torch
from accelerate.utils import set_seed
from diffusers import DDPMScheduler

import library.train_util as train_util
from library.train_util import (
    DreamBoothDataset,
)
import library.config_util as config_util
from library.config_util import (
    ConfigSanitizer,
    BlueprintGenerator,
)
import library.huggingface_util as huggingface_util
import library.custom_train_functions as custom_train_functions
from library.custom_train_functions import apply_snr_weight


# TODO 他のスクリプトと共通化する
def generate_step_logs(args: argparse.Namespace, current_loss, avr_loss, lr_scheduler):
    logs = {"loss/current": current_loss, "loss/average": avr_loss}

    lrs = lr_scheduler.get_last_lr()

    if args.network_train_text_encoder_only or len(lrs) <= 2:  # not block lr (or single block)
        if args.network_train_unet_only:
            logs["lr/unet"] = float(lrs[0])
        elif args.network_train_text_encoder_only:
            logs["lr/textencoder"] = float(lrs[0])
        else:
            logs["lr/textencoder"] = float(lrs[0])
            logs["lr/unet"] = float(lrs[-1])  # may be same to textencoder

        if args.optimizer_type.lower() == "DAdaptation".lower():  # tracking d*lr value of unet.
            logs["lr/d*lr"] = lr_scheduler.optimizers[-1].param_groups[0]["d"] * lr_scheduler.optimizers[-1].param_groups[0]["lr"]
    else:
        idx = 0
        if not args.network_train_unet_only:
            logs["lr/textencoder"] = float(lrs[0])
            idx = 1

        for i in range(idx, len(lrs)):
            logs[f"lr/group{i}"] = float(lrs[i])
            if args.optimizer_type.lower() == "DAdaptation".lower():
                logs[f"lr/d*lr/group{i}"] = (
                    lr_scheduler.optimizers[-1].param_groups[i]["d"] * lr_scheduler.optimizers[-1].param_groups[i]["lr"]
                )

    return logs


def train(args):
    session_id = random.randint(0, 2**32)
    training_started_at = time.time()
    train_util.verify_training_args(args)
    train_util.prepare_dataset_args(args, True)

    cache_latents = args.cache_latents
    use_dreambooth_method = args.in_json is None
    use_user_config = args.dataset_config is not None

    if args.seed is None:
        args.seed = random.randint(0, 2**32)
    set_seed(args.seed)

    tokenizer = train_util.load_tokenizer(args)

    # データセットを準備する
    blueprint_generator = BlueprintGenerator(ConfigSanitizer(True, True, True))
    if use_user_config:
        print(f"Load dataset config from {args.dataset_config}")
        user_config = config_util.load_user_config(args.dataset_config)
        ignored = ["train_data_dir", "reg_data_dir", "in_json"]
        if any(getattr(args, attr) is not None for attr in ignored):
            print(
                "ignore following options because config file is found: {0} / 設定ファイルが利用されるため以下のオプションは無視されます: {0}".format(
                    ", ".join(ignored)
                )
            )
    else:
        if use_dreambooth_method:
            print("Use DreamBooth method.")
            user_config = {
                "datasets": [
                    {"subsets": config_util.generate_dreambooth_subsets_config_by_subdirs(args.train_data_dir, args.reg_data_dir)}
                ]
            }
        else:
            print("Train with captions.")
            user_config = {
                "datasets": [
                    {
                        "subsets": [
                            {
                                "image_dir": args.train_data_dir,
                                "metadata_file": args.in_json,
                            }
                        ]
                    }
                ]
            }

    blueprint = blueprint_generator.generate(user_config, args, tokenizer=tokenizer)
    train_dataset_group = config_util.generate_dataset_group_by_blueprint(blueprint.dataset_group)

    current_epoch = Value("i", 0)
    current_step = Value("i", 0)
    ds_for_collater = train_dataset_group if args.max_data_loader_n_workers == 0 else None
    collater = train_util.collater_class(current_epoch, current_step, ds_for_collater)

    if args.debug_dataset:
        train_util.debug_dataset(train_dataset_group)
        return
    if len(train_dataset_group) == 0:
        print(
            "No data found. Please verify arguments (train_data_dir must be the parent of folders with images) / 画像がありません。引数指定を確認してください（train_data_dirには画像があるフォルダではなく、画像があるフォルダの親フォルダを指定する必要があります）"
        )
        return

    if cache_latents:
        assert (
            train_dataset_group.is_latent_cacheable()
        ), "when caching latents, either color_aug or random_crop cannot be used / latentをキャッシュするときはcolor_augとrandom_cropは使えません"

    # acceleratorを準備する
    print("prepare accelerator")
    accelerator, unwrap_model = train_util.prepare_accelerator(args)
    is_main_process = accelerator.is_main_process

    # mixed precisionに対応した型を用意しておき適宜castする
    weight_dtype, save_dtype = train_util.prepare_dtype(args)

    # モデルを読み込む
    for pi in range(accelerator.state.num_processes):
        # TODO: modify other training scripts as well
        if pi == accelerator.state.local_process_index:
            print(f"loading model for process {accelerator.state.local_process_index}/{accelerator.state.num_processes}")

            text_encoder, vae, unet, _ = train_util.load_target_model(
                args, weight_dtype, accelerator.device if args.lowram else "cpu"
            )

            # work on low-ram device
            if args.lowram:
                text_encoder.to(accelerator.device)
                unet.to(accelerator.device)
                vae.to(accelerator.device)

            gc.collect()
            torch.cuda.empty_cache()
        accelerator.wait_for_everyone()

    # モデルに xformers とか memory efficient attention を組み込む
    train_util.replace_unet_modules(unet, args.mem_eff_attn, args.xformers)

    # 学習を準備する
    if cache_latents:
        vae.to(accelerator.device, dtype=weight_dtype)
        vae.requires_grad_(False)
        vae.eval()
        with torch.no_grad():
            train_dataset_group.cache_latents(vae, args.vae_batch_size)
        vae.to("cpu")
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        gc.collect()

    # prepare network
    import sys

    sys.path.append(os.path.dirname(__file__))
    print("import network module:", args.network_module)
    network_module = importlib.import_module(args.network_module)

    net_kwargs = {}
    if args.network_args is not None:
        for net_arg in args.network_args:
            key, value = net_arg.split("=")
            net_kwargs[key] = value

    # if a new network is added in future, add if ~ then blocks for each network (;'∀')
    network = network_module.create_network(1.0, args.network_dim, args.network_alpha, vae, text_encoder, unet, **net_kwargs)
    if network is None:
        return
    
    if hasattr(network, "prepare_network"):
        network.prepare_network(args)

    train_unet = not args.network_train_text_encoder_only
    train_text_encoder = not args.network_train_unet_only
    network.apply_to(text_encoder, unet, train_text_encoder, train_unet)

    if args.network_weights is not None:
        info = network.load_weights(args.network_weights)
        print(f"load network weights from {args.network_weights}: {info}")

    if args.gradient_checkpointing:
        unet.enable_gradient_checkpointing()
        text_encoder.gradient_checkpointing_enable()
        network.enable_gradient_checkpointing()  # may have no effect

    # 学習に必要なクラスを準備する
    print("prepare optimizer, data loader etc.")

    # 後方互換性を確保するよ
    try:
        trainable_params = network.prepare_optimizer_params(args.text_encoder_lr, args.unet_lr, args.learning_rate)
    except TypeError:
        print("Deprecated: use prepare_optimizer_params(text_encoder_lr, unet_lr, learning_rate) instead of prepare_optimizer_params(text_encoder_lr, unet_lr)")
        trainable_params = network.prepare_optimizer_params(args.text_encoder_lr, args.unet_lr)

    optimizer_name, optimizer_args, optimizer = train_util.get_optimizer(args, trainable_params)

    # dataloaderを準備する
    # DataLoaderのプロセス数：0はメインプロセスになる
    n_workers = min(args.max_data_loader_n_workers, os.cpu_count() - 1)  # cpu_count-1 ただし最大で指定された数まで

    train_dataloader = torch.utils.data.DataLoader(
        train_dataset_group,
        batch_size=1,
        shuffle=True,
        collate_fn=collater,
        num_workers=n_workers,
        persistent_workers=args.persistent_data_loader_workers,
    )

    # 学習ステップ数を計算する
    if args.max_train_epochs is not None:
        args.max_train_steps = args.max_train_epochs * math.ceil(
            len(train_dataloader) / accelerator.num_processes / args.gradient_accumulation_steps
        )
        if is_main_process:
            print(f"override steps. steps for {args.max_train_epochs} epochs is / 指定エポックまでのステップ数: {args.max_train_steps}")

    # データセット側にも学習ステップを送信
    train_dataset_group.set_max_train_steps(args.max_train_steps)

    # lr schedulerを用意する
    lr_scheduler = train_util.get_scheduler_fix(args, optimizer, accelerator.num_processes)

    # 実験的機能：勾配も含めたfp16学習を行う　モデル全体をfp16にする
    if args.full_fp16:
        assert (
            args.mixed_precision == "fp16"
        ), "full_fp16 requires mixed precision='fp16' / full_fp16を使う場合はmixed_precision='fp16'を指定してください。"
        print("enable full fp16 training.")
        network.to(weight_dtype)

    # acceleratorがなんかよろしくやってくれるらしい
    if train_unet and train_text_encoder:
        unet, text_encoder, network, optimizer, train_dataloader, lr_scheduler = accelerator.prepare(
            unet, text_encoder, network, optimizer, train_dataloader, lr_scheduler
        )
    elif train_unet:
        unet, network, optimizer, train_dataloader, lr_scheduler = accelerator.prepare(
            unet, network, optimizer, train_dataloader, lr_scheduler
        )
    elif train_text_encoder:
        text_encoder, network, optimizer, train_dataloader, lr_scheduler = accelerator.prepare(
            text_encoder, network, optimizer, train_dataloader, lr_scheduler
        )
    else:
        network, optimizer, train_dataloader, lr_scheduler = accelerator.prepare(network, optimizer, train_dataloader, lr_scheduler)

    unet.requires_grad_(False)
    unet.to(accelerator.device, dtype=weight_dtype)
    text_encoder.requires_grad_(False)
    text_encoder.to(accelerator.device)
    if args.gradient_checkpointing:  # according to TI example in Diffusers, train is required
        unet.train()
        text_encoder.train()

        # set top parameter requires_grad = True for gradient checkpointing works
        if type(text_encoder) == DDP:
            text_encoder.module.text_model.embeddings.requires_grad_(True)
        else:
            text_encoder.text_model.embeddings.requires_grad_(True)
    else:
        unet.eval()
        text_encoder.eval()

    # support DistributedDataParallel
    if type(text_encoder) == DDP:
        text_encoder = text_encoder.module
        unet = unet.module
        network = network.module

    network.prepare_grad_etc(text_encoder, unet)

    if not cache_latents:
        vae.requires_grad_(False)
        vae.eval()
        vae.to(accelerator.device, dtype=weight_dtype)

    # 実験的機能：勾配も含めたfp16学習を行う　PyTorchにパッチを当ててfp16でのgrad scaleを有効にする
    if args.full_fp16:
        train_util.patch_accelerator_for_fp16_training(accelerator)

    # resumeする
    train_util.resume_from_local_or_hf_if_specified(accelerator, args)

    # epoch数を計算する
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
    num_train_epochs = math.ceil(args.max_train_steps / num_update_steps_per_epoch)
    if (args.save_n_epoch_ratio is not None) and (args.save_n_epoch_ratio > 0):
        args.save_every_n_epochs = math.floor(num_train_epochs / args.save_n_epoch_ratio) or 1

    # 学習する
    # TODO: find a way to handle total batch size when there are multiple datasets
    total_batch_size = args.train_batch_size * accelerator.num_processes * args.gradient_accumulation_steps

    if is_main_process:
        print("running training / 学習開始")
        print(f"  num train images * repeats / 学習画像の数×繰り返し回数: {train_dataset_group.num_train_images}")
        print(f"  num reg images / 正則化画像の数: {train_dataset_group.num_reg_images}")
        print(f"  num batches per epoch / 1epochのバッチ数: {len(train_dataloader)}")
        print(f"  num epochs / epoch数: {num_train_epochs}")
        print(f"  batch size per device / バッチサイズ: {', '.join([str(d.batch_size) for d in train_dataset_group.datasets])}")
        # print(f"  total train batch size (with parallel & distributed & accumulation) / 総バッチサイズ（並列学習、勾配合計含む）: {total_batch_size}")
        print(f"  gradient accumulation steps / 勾配を合計するステップ数 = {args.gradient_accumulation_steps}")
        print(f"  total optimization steps / 学習ステップ数: {args.max_train_steps}")

    # TODO refactor metadata creation and move to util
    metadata = {
        "ss_session_id": session_id,  # random integer indicating which group of epochs the model came from
        "ss_training_started_at": training_started_at,  # unix timestamp
        "ss_output_name": args.output_name,
        "ss_learning_rate": args.learning_rate,
        "ss_text_encoder_lr": args.text_encoder_lr,
        "ss_unet_lr": args.unet_lr,
        "ss_num_train_images": train_dataset_group.num_train_images,
        "ss_num_reg_images": train_dataset_group.num_reg_images,
        "ss_num_batches_per_epoch": len(train_dataloader),
        "ss_num_epochs": num_train_epochs,
        "ss_gradient_checkpointing": args.gradient_checkpointing,
        "ss_gradient_accumulation_steps": args.gradient_accumulation_steps,
        "ss_max_train_steps": args.max_train_steps,
        "ss_lr_warmup_steps": args.lr_warmup_steps,
        "ss_lr_scheduler": args.lr_scheduler,
        "ss_network_module": args.network_module,
        "ss_network_dim": args.network_dim,  # None means default because another network than LoRA may have another default dim
        "ss_network_alpha": args.network_alpha,  # some networks may not use this value
        "ss_mixed_precision": args.mixed_precision,
        "ss_full_fp16": bool(args.full_fp16),
        "ss_v2": bool(args.v2),
        "ss_clip_skip": args.clip_skip,
        "ss_max_token_length": args.max_token_length,
        "ss_cache_latents": bool(args.cache_latents),
        "ss_seed": args.seed,
        "ss_lowram": args.lowram,
        "ss_noise_offset": None if args.noise_offset == 0 else args.noise_offset,
        "ss_training_comment": args.training_comment,  # will not be updated after training
        "ss_sd_scripts_commit_hash": train_util.get_git_revision_hash(),
        "ss_optimizer": optimizer_name + (f"({optimizer_args})" if len(optimizer_args) > 0 else ""),
        "ss_max_grad_norm": args.max_grad_norm,
        "ss_caption_dropout_rate": args.caption_dropout_rate,
        "ss_caption_dropout_every_n_epochs": args.caption_dropout_every_n_epochs,
        "ss_caption_tag_dropout_rate": args.caption_tag_dropout_rate,
        "ss_face_crop_aug_range": args.face_crop_aug_range,
        "ss_prior_loss_weight": args.prior_loss_weight,
        "ss_min_snr_gamma": args.min_snr_gamma,
        "ss_multires_noise_discount": None if args.multires_noise_discount == 0 else args.multires_noise_discount,
        "ss_multires_noise_iterations": None if args.multires_noise_iterations == 0 else args.multires_noise_iterations,
        "ss_ip_noise_gamma": getattr(args, "ip_noise_gamma", None),
        "ss_network_dropout": getattr(args, "network_dropout", None),
    }

    if use_user_config:
        # save metadata of multiple datasets
        # NOTE: pack "ss_datasets" value as json one time
        #   or should also pack nested collections as json?
        datasets_metadata = []
        tag_frequency = {}  # merge tag frequency for metadata editor
        dataset_dirs_info = {}  # merge subset dirs for metadata editor

        for dataset in train_dataset_group.datasets:
            is_dreambooth_dataset = isinstance(dataset, DreamBoothDataset)
            dataset_metadata = {
                "is_dreambooth": is_dreambooth_dataset,
                "batch_size_per_device": dataset.batch_size,
                "num_train_images": dataset.num_train_images,  # includes repeating
                "num_reg_images": dataset.num_reg_images,
                "resolution": (dataset.width, dataset.height),
                "enable_bucket": bool(dataset.enable_bucket),
                "min_bucket_reso": dataset.min_bucket_reso,
                "max_bucket_reso": dataset.max_bucket_reso,
                "tag_frequency": dataset.tag_frequency,
                "bucket_info": dataset.bucket_info,
            }

            subsets_metadata = []
            for subset in dataset.subsets:
                subset_metadata = {
                    "img_count": subset.img_count,
                    "num_repeats": subset.num_repeats,
                    "color_aug": bool(subset.color_aug),
                    "flip_aug": bool(subset.flip_aug),
                    "random_crop": bool(subset.random_crop),
                    "shuffle_caption": bool(subset.shuffle_caption),
                    "keep_tokens": subset.keep_tokens,
                }

                image_dir_or_metadata_file = None
                if subset.image_dir:
                    image_dir = os.path.basename(subset.image_dir)
                    subset_metadata["image_dir"] = image_dir
                    image_dir_or_metadata_file = image_dir

                if is_dreambooth_dataset:
                    subset_metadata["class_tokens"] = subset.class_tokens
                    subset_metadata["is_reg"] = subset.is_reg
                    if subset.is_reg:
                        image_dir_or_metadata_file = None  # not merging reg dataset
                else:
                    metadata_file = os.path.basename(subset.metadata_file)
                    subset_metadata["metadata_file"] = metadata_file
                    image_dir_or_metadata_file = metadata_file  # may overwrite

                subsets_metadata.append(subset_metadata)

                # merge dataset dir: not reg subset only
                # TODO update additional-network extension to show detailed dataset config from metadata
                if image_dir_or_metadata_file is not None:
                    # datasets may have a certain dir multiple times
                    v = image_dir_or_metadata_file
                    i = 2
                    while v in dataset_dirs_info:
                        v = image_dir_or_metadata_file + f" ({i})"
                        i += 1
                    image_dir_or_metadata_file = v

                    dataset_dirs_info[image_dir_or_metadata_file] = {"n_repeats": subset.num_repeats, "img_count": subset.img_count}

            dataset_metadata["subsets"] = subsets_metadata
            datasets_metadata.append(dataset_metadata)

            # merge tag frequency:
            for ds_dir_name, ds_freq_for_dir in dataset.tag_frequency.items():
                # あるディレクトリが複数のdatasetで使用されている場合、一度だけ数える
                # もともと繰り返し回数を指定しているので、キャプション内でのタグの出現回数と、それが学習で何度使われるかは一致しない
                # なので、ここで複数datasetの回数を合算してもあまり意味はない
                if ds_dir_name in tag_frequency:
                    continue
                tag_frequency[ds_dir_name] = ds_freq_for_dir

        metadata["ss_datasets"] = json.dumps(datasets_metadata)
        metadata["ss_tag_frequency"] = json.dumps(tag_frequency)
        metadata["ss_dataset_dirs"] = json.dumps(dataset_dirs_info)
    else:
        # conserving backward compatibility when using train_dataset_dir and reg_dataset_dir
        assert (
            len(train_dataset_group.datasets) == 1
        ), f"There should be a single dataset but {len(train_dataset_group.datasets)} found. This seems to be a bug. / データセットは1個だけ存在するはずですが、実際には{len(train_dataset_group.datasets)}個でした。プログラムのバグかもしれません。"

        dataset = train_dataset_group.datasets[0]

        dataset_dirs_info = {}
        reg_dataset_dirs_info = {}
        if use_dreambooth_method:
            for subset in dataset.subsets:
                info = reg_dataset_dirs_info if subset.is_reg else dataset_dirs_info
                info[os.path.basename(subset.image_dir)] = {"n_repeats": subset.num_repeats, "img_count": subset.img_count}
        else:
            for subset in dataset.subsets:
                dataset_dirs_info[os.path.basename(subset.metadata_file)] = {
                    "n_repeats": subset.num_repeats,
                    "img_count": subset.img_count,
                }

        metadata.update(
            {
                "ss_batch_size_per_device": args.train_batch_size,
                "ss_total_batch_size": total_batch_size,
                "ss_resolution": args.resolution,
                "ss_color_aug": bool(args.color_aug),
                "ss_flip_aug": bool(args.flip_aug),
                "ss_random_crop": bool(args.random_crop),
                "ss_shuffle_caption": bool(args.shuffle_caption),
                "ss_enable_bucket": bool(dataset.enable_bucket),
                "ss_bucket_no_upscale": bool(dataset.bucket_no_upscale),
                "ss_min_bucket_reso": dataset.min_bucket_reso,
                "ss_max_bucket_reso": dataset.max_bucket_reso,
                "ss_keep_tokens": args.keep_tokens,
                "ss_dataset_dirs": json.dumps(dataset_dirs_info),
                "ss_reg_dataset_dirs": json.dumps(reg_dataset_dirs_info),
                "ss_tag_frequency": json.dumps(dataset.tag_frequency),
                "ss_bucket_info": json.dumps(dataset.bucket_info),
            }
        )

    # add extra args
    if args.network_args:
        metadata["ss_network_args"] = json.dumps(net_kwargs)

    # model name and hash
    if args.pretrained_model_name_or_path is not None:
        sd_model_name = args.pretrained_model_name_or_path
        if os.path.exists(sd_model_name):
            metadata["ss_sd_model_hash"] = train_util.model_hash(sd_model_name)
            metadata["ss_new_sd_model_hash"] = train_util.calculate_sha256(sd_model_name)
            sd_model_name = os.path.basename(sd_model_name)
        metadata["ss_sd_model_name"] = sd_model_name

    if args.vae is not None:
        vae_name = args.vae
        if os.path.exists(vae_name):
            # metadata["ss_vae_hash"] = train_util.model_hash(vae_name)
            # metadata["ss_new_vae_hash"] = train_util.calculate_sha256(vae_name)
            vae_name = os.path.basename(vae_name)
        # metadata["ss_vae_name"] = vae_name

    metadata = {k: str(v) for k, v in metadata.items()}

    # make minimum metadata for filtering
    minimum_keys = ["ss_network_module", "ss_network_dim", "ss_network_alpha", "ss_network_args"]
    minimum_metadata = {}
    for key in minimum_keys:
        if key in metadata:
            minimum_metadata[key] = metadata[key]

    progress_bar = tqdm(range(args.max_train_steps), smoothing=0, disable=not accelerator.is_local_main_process, desc="steps")
    global_step = 0

    noise_scheduler = DDPMScheduler(
        beta_start=0.00085, beta_end=0.012, beta_schedule="scaled_linear", num_train_timesteps=1000, clip_sample=False
    )
    if accelerator.is_main_process:
        accelerator.init_trackers("network_train")

    loss_list = []
    loss_total = 0.0
    del train_dataset_group
    for epoch in range(num_train_epochs):
        if is_main_process:
            print(f"epoch {epoch+1}/{num_train_epochs}")
        current_epoch.value = epoch + 1

        metadata["ss_epoch"] = str(epoch + 1)

        network.on_epoch_start(text_encoder, unet)

        for step, batch in enumerate(train_dataloader):
            current_step.value = global_step
            with accelerator.accumulate(network):
                with torch.no_grad():
                    if "latents" in batch and batch["latents"] is not None:
                        latents = batch["latents"].to(accelerator.device)
                    else:
                        # latentに変換
                        latents = vae.encode(batch["images"].to(dtype=weight_dtype)).latent_dist.sample()
                    latents = latents * 0.18215
                b_size = latents.shape[0]

                with torch.set_grad_enabled(train_text_encoder):
                    # Get the text embedding for conditioning
                    input_ids = batch["input_ids"].to(accelerator.device)
                    encoder_hidden_states = train_util.get_hidden_states(args, input_ids, tokenizer, text_encoder, weight_dtype)

                # Sample noise that we'll add to the latents
                noise = torch.randn_like(latents, device=latents.device)
                if args.noise_offset:
                    # https://www.crosslabs.org//blog/diffusion-with-offset-noise
                    noise += args.noise_offset * torch.randn((latents.shape[0], latents.shape[1], 1, 1), device=latents.device)

                # Sample a random timestep for each image
                timesteps = torch.randint(0, noise_scheduler.config.num_train_timesteps, (b_size,), device=latents.device)
                timesteps = timesteps.long()
                # Add noise to the latents according to the noise magnitude at each timestep
                # (this is the forward diffusion process)
                noisy_latents = noise_scheduler.add_noise(latents, noise, timesteps)

                # Predict the noise residual
                with accelerator.autocast():
                    noise_pred = unet(noisy_latents, timesteps, encoder_hidden_states).sample

                if args.v_parameterization:
                    # v-parameterization training
                    target = noise_scheduler.get_velocity(latents, noise, timesteps)
                else:
                    target = noise

                loss = torch.nn.functional.mse_loss(noise_pred.float(), target.float(), reduction="none")
                loss = loss.mean([1, 2, 3])

                loss_weights = batch["loss_weights"]  # 各sampleごとのweight
                loss = loss * loss_weights

                if args.min_snr_gamma:
                    loss = apply_snr_weight(loss, timesteps, noise_scheduler, args.min_snr_gamma)

                loss = loss.mean()  # 平均なのでbatch_sizeで割る必要なし

                accelerator.backward(loss)
                if accelerator.sync_gradients and args.max_grad_norm != 0.0:
                    params_to_clip = network.get_trainable_params()
                    accelerator.clip_grad_norm_(params_to_clip, args.max_grad_norm)

                optimizer.step()
                lr_scheduler.step()
                optimizer.zero_grad(set_to_none=True)

            # Checks if the accelerator has performed an optimization step behind the scenes
            if accelerator.sync_gradients:
                progress_bar.update(1)
                global_step += 1

                train_util.sample_images(
                    accelerator, args, None, global_step, accelerator.device, vae, tokenizer, text_encoder, unet
                )

            current_loss = loss.detach().item()
            if epoch == 0:
                loss_list.append(current_loss)
            else:
                loss_total -= loss_list[step]
                loss_list[step] = current_loss
            loss_total += current_loss
            avr_loss = loss_total / len(loss_list)
            logs = {"loss": avr_loss}  # , "lr": lr_scheduler.get_last_lr()[0]}
            progress_bar.set_postfix(**logs)

            if args.logging_dir is not None:
                logs = generate_step_logs(args, current_loss, avr_loss, lr_scheduler)
                accelerator.log(logs, step=global_step)

            if global_step >= args.max_train_steps:
                break

        if args.logging_dir is not None:
            logs = {"loss/epoch": loss_total / len(loss_list)}
            accelerator.log(logs, step=epoch + 1)

        accelerator.wait_for_everyone()

        if args.save_every_n_epochs is not None:
            model_name = train_util.DEFAULT_EPOCH_NAME if args.output_name is None else args.output_name

            def save_func():
                ckpt_name = train_util.EPOCH_FILE_NAME.format(model_name, epoch + 1) + "." + args.save_model_as
                ckpt_file = os.path.join(args.output_dir, ckpt_name)
                metadata["ss_steps"] = str(global_step)
                metadata["ss_training_finished_at"] = str(time.time())
                print(f"saving checkpoint: {ckpt_file}")
                unwrap_model(network).save_weights(ckpt_file, save_dtype, minimum_metadata if args.no_metadata else metadata)
                if args.huggingface_repo_id is not None:
                    huggingface_util.upload(args, ckpt_file, "/" + ckpt_name)

            def remove_old_func(old_epoch_no):
                old_ckpt_name = train_util.EPOCH_FILE_NAME.format(model_name, old_epoch_no) + "." + args.save_model_as
                old_ckpt_file = os.path.join(args.output_dir, old_ckpt_name)
                if os.path.exists(old_ckpt_file):
                    print(f"removing old checkpoint: {old_ckpt_file}")
                    os.remove(old_ckpt_file)

            if is_main_process:
                saving = train_util.save_on_epoch_end(args, save_func, remove_old_func, epoch + 1, num_train_epochs)
                if saving and args.save_state:
                    train_util.save_state_on_epoch_end(args, accelerator, model_name, epoch + 1)

        train_util.sample_images(accelerator, args, epoch + 1, global_step, accelerator.device, vae, tokenizer, text_encoder, unet)

        # end of epoch

    metadata["ss_epoch"] = str(num_train_epochs)
    metadata["ss_training_finished_at"] = str(time.time())

    if is_main_process:
        network = unwrap_model(network)

    accelerator.end_training()

    if args.save_state:
        train_util.save_state_on_train_end(args, accelerator)

    del accelerator  # この後メモリを使うのでこれは消す

    if is_main_process:
        os.makedirs(args.output_dir, exist_ok=True)

        model_name = train_util.DEFAULT_LAST_OUTPUT_NAME if args.output_name is None else args.output_name
        ckpt_name = model_name + "." + args.save_model_as
        ckpt_file = os.path.join(args.output_dir, ckpt_name)

        print(f"save trained model to {ckpt_file}")
        network.save_weights(ckpt_file, save_dtype, minimum_metadata if args.no_metadata else metadata)
        if args.huggingface_repo_id is not None:
            huggingface_util.upload(args, ckpt_file, "/" + ckpt_name, force_sync_upload=True)
        print("model saved.")


def setup_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()

    train_util.add_sd_models_arguments(parser)
    train_util.add_dataset_arguments(parser, True, True, True)
    train_util.add_training_arguments(parser, True)
    train_util.add_optimizer_arguments(parser)
    config_util.add_config_arguments(parser)
    custom_train_functions.add_custom_train_arguments(parser)

    parser.add_argument("--no_metadata", action="store_true", help="do not save metadata in output model / メタデータを出力先モデルに保存しない")
    parser.add_argument(
        "--save_model_as",
        type=str,
        default="safetensors",
        choices=[None, "ckpt", "pt", "safetensors"],
        help="format to save the model (default is .safetensors) / モデル保存時の形式（デフォルトはsafetensors）",
    )

    parser.add_argument("--unet_lr", type=float, default=None, help="learning rate for U-Net / U-Netの学習率")
    parser.add_argument("--text_encoder_lr", type=float, default=None, help="learning rate for Text Encoder / Text Encoderの学習率")

    parser.add_argument("--network_weights", type=str, default=None, help="pretrained weights for network / 学習するネットワークの初期重み")
    parser.add_argument("--network_module", type=str, default=None, help="network module to train / 学習対象のネットワークのモジュール")
    parser.add_argument(
        "--network_dim", type=int, default=None, help="network dimensions (depends on each network) / モジュールの次元数（ネットワークにより定義は異なります）"
    )
    parser.add_argument(
        "--network_alpha",
        type=float,
        default=1,
        help="alpha for LoRA weight scaling, default 1 (same as network_dim for same behavior as old version) / LoRaの重み調整のalpha値、デフォルト1（旧バージョンと同じ動作をするにはnetwork_dimと同じ値を指定）",
    )
    parser.add_argument(
        "--network_args", type=str, default=None, nargs="*", help="additional argmuments for network (key=value) / ネットワークへの追加の引数"
    )
    parser.add_argument("--network_train_unet_only", action="store_true", help="only training U-Net part / U-Net関連部分のみ学習する")
    parser.add_argument(
        "--network_train_text_encoder_only", action="store_true", help="only training Text Encoder part / Text Encoder関連部分のみ学習する"
    )
    parser.add_argument(
        "--training_comment", type=str, default=None, help="arbitrary comment string stored in metadata / メタデータに記録する任意のコメント文字列"
    )

    return parser


if __name__ == "__main__":
    parser = setup_parser()

    args = parser.parse_args()
    args = train_util.read_config_from_file(args, parser)

    train(args)
