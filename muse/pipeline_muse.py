# Copyright 2023 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
from typing import List, Optional, Union

import numpy as np
import torch
from PIL import Image
from transformers import (
    AutoTokenizer,
    CLIPTextModel,
    PreTrainedTokenizer,
    T5EncoderModel,
)

from .modeling_maskgit_vqgan import MaskGitVQGAN
from .modeling_movq import MOVQ
from .modeling_taming_vqgan import VQGANModel
from .modeling_transformer import MaskGitTransformer, MaskGiTUViT


class PipelineMuse:
    def __init__(
        self,
        vae: Union[VQGANModel, MOVQ, MaskGitVQGAN],
        transformer: Union[MaskGitTransformer, MaskGiTUViT],
        is_class_conditioned: bool = False,
        text_encoder: Optional[Union[T5EncoderModel, CLIPTextModel]] = None,
        tokenizer: Optional[PreTrainedTokenizer] = None,
    ) -> None:
        self.text_encoder = text_encoder
        self.tokenizer = tokenizer
        self.vae = vae
        self.transformer = transformer
        self.is_class_conditioned = is_class_conditioned
        self.device = "cpu"

    def to(self, device="cpu", dtype=torch.float32) -> None:
        if not self.is_class_conditioned:
            self.text_encoder.to(device, dtype=dtype)
        self.vae.to(device, dtype=dtype)
        self.transformer.to(device, dtype=dtype)
        self.device = device
        self.dtype = dtype
        return self

    @torch.no_grad()
    def __call__(
        self,
        text: Optional[Union[str, List[str]]] = None,
        negative_text: Optional[Union[str, List[str]]] = None,
        class_ids: Optional[Union[int, List[int]]] = None,
        timesteps: int = 8,
        guidance_scale: float = 8.0,
        temperature: float = 1.0,
        topk_filter_thres: float = 0.9,
        num_images_per_prompt: int = 1,
        use_maskgit_generate: bool = True,
        generator: Optional[torch.Generator] = None,
        use_fp16: bool = False,
    ):
        if text is None and class_ids is None:
            raise ValueError("Either text or class_ids must be provided.")

        if text is not None and class_ids is not None:
            raise ValueError("Only one of text or class_ids may be provided.")

        if class_ids is not None:
            if isinstance(class_ids, int):
                class_ids = [class_ids]

            class_ids = torch.tensor(class_ids, device=self.device, dtype=torch.long)
            # duplicate class ids for each generation per prompt
            class_ids = class_ids.repeat_interleave(num_images_per_prompt, dim=0)
            model_inputs = {"class_ids": class_ids}
        else:
            if isinstance(text, str):
                text = [text]

            input_ids = self.tokenizer(
                text,
                return_tensors="pt",
                padding="max_length",
                truncation=True,
                max_length=self.tokenizer.model_max_length,
            ).input_ids  # TODO: remove hardcode
            input_ids = input_ids.to(self.device)
            encoder_hidden_states = self.text_encoder(input_ids).last_hidden_state

            if negative_text is not None:
                if isinstance(negative_text, str):
                    negative_text = [negative_text]

                negative_input_ids = self.tokenizer(
                    negative_text,
                    return_tensors="pt",
                    padding="max_length",
                    truncation=True,
                    max_length=self.tokenizer.model_max_length,
                ).input_ids
                negative_input_ids = negative_input_ids.to(self.device)
                negative_encoder_hidden_states = self.text_encoder(negative_input_ids).last_hidden_state
            else:
                negative_encoder_hidden_states = None

            # duplicate text embeddings for each generation per prompt, using mps friendly method
            bs_embed, seq_len, _ = encoder_hidden_states.shape
            encoder_hidden_states = encoder_hidden_states.repeat(1, num_images_per_prompt, 1)
            encoder_hidden_states = encoder_hidden_states.view(bs_embed * num_images_per_prompt, seq_len, -1)
            if negative_encoder_hidden_states is not None:
                bs_embed, seq_len, _ = negative_encoder_hidden_states.shape
                negative_encoder_hidden_states = negative_encoder_hidden_states.repeat(1, num_images_per_prompt, 1)
                negative_encoder_hidden_states = negative_encoder_hidden_states.view(
                    bs_embed * num_images_per_prompt, seq_len, -1
                )

            model_inputs = {
                "encoder_hidden_states": encoder_hidden_states,
                "negative_embeds": negative_encoder_hidden_states,
            }

        generate = self.transformer.generate
        if use_maskgit_generate:
            generate = self.transformer.generate2

        with torch.autocast("cuda", enabled=use_fp16):
            generated_tokens = generate(
                **model_inputs,
                timesteps=timesteps,
                guidance_scale=guidance_scale,
                temperature=temperature,
                topk_filter_thres=topk_filter_thres,
                generator=generator,
            )

        images = self.vae.decode_code(generated_tokens)

        # Convert to PIL images
        images = [self.to_pil_image(image) for image in images]
        return images

    def to_pil_image(self, image: torch.Tensor):
        image = image.permute(1, 2, 0).cpu().numpy()
        image = 2.0 * image - 1.0
        image = np.clip(image, -1.0, 1.0)
        image = (image + 1.0) / 2.0
        image = (255 * image).astype(np.uint8)
        image = Image.fromarray(image).convert("RGB")
        return image

    @classmethod
    def from_pretrained(
        cls,
        model_name_or_path: str = None,
        text_encoder_path: Optional[str] = None,
        vae_path: Optional[str] = None,
        transformer_path: Optional[str] = None,
        is_class_conditioned: bool = False,
    ) -> None:
        """
        Instantiate a PipelineMuse from a pretrained model. Either model_name_or_path or all of text_encoder_path, vae_path, and
        transformer_path must be provided.
        """
        if model_name_or_path is None:
            if text_encoder_path is None or vae_path is None or transformer_path is None:
                raise ValueError(
                    "If model_name_or_path is None, then text_encoder_path, vae_path, and transformer_path must be"
                    " provided."
                )

            text_encoder_args = None
            tokenizer_args = None

            if not is_class_conditioned:
                text_encoder_args = {"pretrained_model_name_or_path": text_encoder_path}
                tokenizer_args = {"pretrained_model_name_or_path": text_encoder_path}

            vae_args = {"pretrained_model_name_or_path": vae_path}
            transformer_args = {"pretrained_model_name_or_path": transformer_path}
        else:
            text_encoder_args = None
            tokenizer_args = None

            if not is_class_conditioned:
                text_encoder_args = {"pretrained_model_name_or_path": model_name_or_path, "subfolder": "text_encoder"}
                tokenizer_args = {"pretrained_model_name_or_path": model_name_or_path, "subfolder": "text_encoder"}

            vae_args = {"pretrained_model_name_or_path": model_name_or_path, "subfolder": "vae"}
            transformer_args = {"pretrained_model_name_or_path": model_name_or_path, "subfolder": "transformer"}

        if not is_class_conditioned:
            # Very hacky way to load different text encoders
            # TODO: Add config for pipeline to specify text encoder
            is_clip = "clip" in text_encoder_args["pretrained_model_name_or_path"]
            text_encoder_cls = CLIPTextModel if is_clip else T5EncoderModel
            
            text_encoder = text_encoder_cls.from_pretrained(**text_encoder_args)
            tokenizer = AutoTokenizer.from_pretrained(**tokenizer_args)

        transformer_config = MaskGitTransformer.load_config(**transformer_args)
        if transformer_config["_class_name"] == "MaskGitTransformer":
            transformer = MaskGitTransformer.from_pretrained(**transformer_args)
        elif transformer_config["_class_name"] == "MaskGiTUViT":
            transformer = MaskGiTUViT.from_pretrained(**transformer_args)
        else:
            raise ValueError(f"Unknown Transformer class: {transformer_config['_class_name']}")

        # Hacky way to load different VQ models
        vae_config = MaskGitVQGAN.load_config(**vae_args)
        if vae_config["_class_name"] == "VQGANModel":
            vae = VQGANModel.from_pretrained(**vae_args)
        elif vae_config["_class_name"] == "MaskGitVQGAN":
            vae = MaskGitVQGAN.from_pretrained(**vae_args)
        elif vae_config["_class_name"] == "MOVQ":
            vae = MOVQ.from_pretrained(**vae_args)
        else:
            raise ValueError(f"Unknown VAE class: {vae_config['_class_name']}")

        return cls(
            vae=vae,
            transformer=transformer,
            text_encoder=text_encoder,
            tokenizer=tokenizer,
            is_class_conditioned=is_class_conditioned,
        )

    def save_pretrained(
        self,
        save_directory: Union[str, os.PathLike],
    ) -> None:
        """
        Save the pipeline's model and tokenizer to the specified directory.
        """

        if not self.is_class_conditioned:
            self.text_encoder.save_pretrained(os.path.join(save_directory, "text_encoder"))
            self.tokenizer.save_pretrained(os.path.join(save_directory, "text_encoder"))

        self.vae.save_pretrained(os.path.join(save_directory, "vae"))
        self.transformer.save_pretrained(os.path.join(save_directory, "transformer"))
