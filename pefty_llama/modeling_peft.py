# based on https://github.com/zphang/minimal-llama/blob/c37e481136f118a16f77f50cdf5e867ed5dafbf9/minimal_llama/pref/llama_simple2.py

import os
import json
import math
import dataclasses

import torch
import torch.nn as nn
import torch.nn.functional as F

import bitsandbytes as bnb
import tqdm.auto as tqdm

from accelerate import init_empty_weights
from transformers.utils.bitsandbytes import set_module_8bit_tensor_to_device
from transformers import (
    LlamaConfig as HF_LlamaConfig,
    LlamaForCausalLM as HF_Llama,
)
import pefty_llama.peft as peft
from pefty_llama.configuration import LLaMAConfig, LLAMA_CONFIG_DICT


class LLaMAModel(nn.Module):
    def __init__(self, config: LLaMAConfig, peft_config: peft.PeftConfig):
        super().__init__()
        self.config = config
        self.peft_config = peft_config
        self.model = LLaMAInnerModel(config=config, peft_config=peft_config)
        self.lm_head = NoInitLinear(config.dim, config.vocab_size, bias=False, dtype=config.dtype)

        if self.peft_config.peft_mode == peft.PEFT_PREFIX:
            self.peft_prefixes = peft.SoftPrefixes(config=config, peft_config=peft_config)

    @classmethod
    def from_pretrained(cls, model_name_or_path, use_8bit=False):
        """Load model from a huggingface model name or path."""
        hf_config = HF_LlamaConfig.from_pretrained(model_name_or_path)

        config = LLaMAConfig(
            vocab_size=hf_config.vocab_size,
            dim=hf_config.hidden_size,
            n_layers=hf_config.num_hidden_layers,
            n_heads=hf_config.num_attention_heads,
            max_seq_length=hf_config.max_position_embeddings,
            dtype=hf_config.dtype,
            pad_token_id=hf_config.pad_token_id,
            bos_token_id=hf_config.bos_token_id,
            eos_token_id=hf_config.eos_token_id,
            use_8bit=use_8bit,
        )

        raise NotImplementedError()
        model = cls(config)

        # Load weights from huggingface model to the disk if needed
        if os.path.isdir(model_name_or_path):
            hf_model_path = model_name_or_path
        else:
            hf_model_path = hf_config.cache_dir
            hf_model = HF_LLaMA.from_pretrained(hf_model_path, config=hf_config)
            hf_model.save_pretrained(hf_model_path)

        return model

    def forward(self,
                input_ids):
        """Forward pass (with full decode sequence, intended for training or loss-scoring)

        :param input_ids: [batch_size, seq_len]
        :return: logits [batch_size, seq_len]
        """
        # 1) Create masks
        # decoder mask
        # [batch_size, num_heads=1, q_len=seq_len, kv_len=seq_len]
        attention_mask = create_attention_mask(input_ids=input_ids, dtype=self.config.dtype)
        input_ids_for_rope = input_ids
        if self.peft_config.peft_mode == peft.PEFT_PREFIX:
            attention_mask = torch.cat([
                zeros_like([1, 1, input_ids.shape[1], self.peft_config.num_prefix_tokens], tensor=attention_mask),
                attention_mask,
            ], dim=3)

        if self.peft_config.peft_mode in peft.PEFT_PROMPT:
            input_ids_for_rope = torch.cat([
                torch.ones([input_ids.shape[0], self.peft_config.num_prefix_tokens],
                           dtype=input_ids.dtype, device=input_ids.device),
                input_ids,
            ], dim=1)
            # Easier to just remake the attention mask
            attention_mask = create_attention_mask(input_ids=input_ids_for_rope, dtype=self.config.dtype)
        rope_embed_ids = create_rope_embed_ids(input_ids=input_ids_for_rope)
        cos, sin = self.get_cos_sin(rope_embed_ids)

        if self.peft_config.peft_mode == peft.PEFT_PREFIX:
            kv_cache = self.peft_prefixes(batch_size=input_ids.shape[0])
        else:
            kv_cache = None

        # 2) Forward pass
        # [batch_size, seq_len, hidden_dim]
        model_out = self.model(
            input_ids,
            attention_mask=attention_mask,
            cos=cos, sin=sin,
            kv_cache=kv_cache,
        )
        # [batch_size, seq_len, vocab_size]
        logits = self.lm_head(model_out["hidden_states"])
        return logits

    def init_kv_cache(self, input_ids):
        # noinspection GrazieInspection
        """Initialize KV cache for decoding.

        A KV cache consists of a list of dicts (one per layer):
            dict(
              key = [batch_size, num_heads, kv_seq_len=0, head_dim]
              value = [batch_size, num_heads, kv_seq_len=0, head_dim]
            )

        :param input_ids: [batch_size, dec_seq_len]
        :return: 0-length kv_cache
        """
        kv_cache = []
        batch_size = input_ids.shape[0]
        num_heads = self.config.n_heads
        head_dim = self.config.head_dim
        for layer in self.model.layers:
            device = layer.input_layernorm.weight.device
            kv_cache.append({
                "key": torch.zeros([batch_size, num_heads, 0, head_dim]).to(device=device, dtype=self.config.dtype),
                "value": torch.zeros([batch_size, num_heads, 0, head_dim]).to(device=device, dtype=self.config.dtype),
            })
        return kv_cache

    def generate(self, input_ids, generation_length: 20):
        """Generate tokens with efficient caching of KV.

        TODO: Add stopping conditions
        TODO: Add sampling capabilities

        :param input_ids: [batch_size, enc_seq_len]
        :param generation_length: int
        :return: [batch_size, generation_length]
        """
        original_input_ids = input_ids
        batch_size, seq_len = input_ids.shape
        # noinspection PyUnresolvedReferences
        num_valid_tokens = (input_ids != self.config.pad_token_id).long().sum(dim=1)

        # 1) Setup
        if input_ids is None:
            # [batch_size, dec_seq_len=1]
            input_ids = torch.LongTensor(
                [[self.config.pad_token_id]] * batch_size
            ).to(self.lm_head.weights.device)
        # See: init_kv_cache. list[dict]
        if self.peft_config.peft_mode == peft.PEFT_PREFIX:
            kv_cache = self.peft_prefixes(batch_size=input_ids.shape[0])
            num_valid_kv_cache = num_valid_tokens + self.peft_config.num_prefix_tokens
        else:
            kv_cache = self.init_kv_cache(input_ids)
            num_valid_kv_cache = num_valid_tokens
        generated_token_ids_list = [original_input_ids]
        total_seq_len = seq_len

        # 2) First encoding
        # [batch_size=1, num_heads=1, q_len=1, kv_len=1]
        attention_mask = create_attention_mask(input_ids=input_ids, dtype=self.config.dtype)
        input_ids_for_rope = input_ids
        # dict(
        #   hidden_states = [batch_size, dec_seq_len=decode_step+1, hidden_dim]
        #   kv_cache = list[dict(
        #     key = [batch_size, num_heads, kv_seq_len=decode_step+1, head_dim]
        #     value = [batch_size, num_heads, kv_seq_len=decode_step+1, head_dim]
        #   )]
        # )
        if self.peft_config.peft_mode in (peft.PEFT_PREFIX, peft.PEFT_PROMPT):
            num_prefix_tokens = self.peft_config.num_prefix_tokens
            total_seq_len += num_prefix_tokens
            # [batch_size, num_heads=1, q_len=seq_len, kv_len=num_prefix_tokens + dec_seq_len]
            attention_mask = torch.cat([
                zeros_like([1, 1, input_ids.shape[1], num_prefix_tokens], tensor=attention_mask),
                attention_mask,
            ], dim=3)

        if self.peft_config.peft_mode in peft.PEFT_PROMPT:
            input_ids_for_rope = torch.cat([
                torch.ones([input_ids.shape[0], self.peft_config.num_prefix_tokens],
                           dtype=input_ids.dtype, device=input_ids.device),
                input_ids,
            ], dim=1)
            # Easier to just remake the attention mask
            attention_mask = create_attention_mask(input_ids=input_ids_for_rope, dtype=self.config.dtype)
        rope_embed_ids = create_rope_embed_ids(input_ids=input_ids_for_rope)
        cos, sin = self.get_cos_sin(rope_embed_ids)
        model_out = self.model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            cos=cos, sin=sin,
            kv_cache=kv_cache,
        )
        logits = self.lm_head(model_out["hidden_states"])
        kv_cache = model_out["kv_cache"]
        generated_token_ids = logits.argmax(-1)[
            torch.arange(batch_size, dtype=torch.long, device=input_ids.device),
            num_valid_tokens-1,
        ][:, None]
        generated_token_ids_list.append(generated_token_ids)
        input_ids = generated_token_ids

        # 2.1 shift KV cache
        for layer_kv_cache in kv_cache:
            for i in range(batch_size):
                layer_kv_cache["key"] = shift_kv_cache_right(
                    layer_kv_cache["key"], num_valid_tokens=num_valid_kv_cache)
                layer_kv_cache["value"] = shift_kv_cache_right(
                    layer_kv_cache["value"], num_valid_tokens=num_valid_kv_cache)

        # 3) Subsequent steps
        for decode_step in range(generation_length-1):
            num_valid_tokens += 1
            total_seq_len += 1
            # [batch_size=1, num_heads=1, q_len=1, kv_len=1]
            attention_mask = convert_mask_to_soft_mask(create_generation_attention_mask(
                batch_size=batch_size,
                seq_len=total_seq_len,
                num_valid_tokens=num_valid_tokens,
                device=input_ids.device,
            ), dtype=self.config.dtype)
            # dict(
            #   hidden_states = [batch_size, dec_seq_len=decode_step+1, hidden_dim]
            #   kv_cache = list[dict(
            #     key = [batch_size, num_heads, kv_seq_len=decode_step+1, head_dim]
            #     value = [batch_size, num_heads, kv_seq_len=decode_step+1, head_dim]
            #   )]
            # )
            rope_embed_ids = create_rope_embed_ids(input_ids=input_ids) + num_valid_tokens
            cos, sin = self.get_cos_sin(rope_embed_ids)
            model_out = self.model(
                input_ids=input_ids,
                attention_mask=attention_mask,
                kv_cache=kv_cache,
                cos=cos, sin=sin,
            )
            # [batch_size, dec_seq_len=1, vocab_size]
            logits = self.lm_head(model_out["hidden_states"])
            kv_cache = model_out["kv_cache"]
            # [batch_size, dec_seq_len=1]
            generated_token_ids = logits.argmax(-1)[:, -1:]
            generated_token_ids_list.append(generated_token_ids)
            input_ids = generated_token_ids
        return torch.cat(generated_token_ids_list, dim=1)

    def get_cos_sin(self, rope_embed_ids):
        cos = F.embedding(
            rope_embed_ids,
            self.model.layers[0].self_attn.rotary_emb.cos_cached[0, 0]
        ).to(self.config.dtype)
        sin = F.embedding(
            rope_embed_ids,
            self.model.layers[0].self_attn.rotary_emb.sin_cached[0, 0]
        ).to(self.config.dtype)
        cos, sin = cos[:, None, :, :], sin[:, None, :, :]
        return cos, sin

    def gradient_checkpointing_enable(self):
        self.config.gradient_checkpointing = True

    def enable_input_require_grads(self):
        def make_inputs_require_grads(module, input, output):
            output.requires_grad_(True)
        self.model.embed_tokens.register_forward_hook(make_inputs_require_grads)



class LLaMAInnerModel(nn.Module):
    def __init__(self, config: LLaMAConfig, peft_config: peft.PeftConfig):
        super().__init__()
        self.config = config
        self.peft_config = peft_config
        self.embed_tokens = nn.Embedding(config.vocab_size, config.dim, dtype=config.dtype)
        self.layers = nn.ModuleList([
            LLaMALayer(config=config, peft_config=peft_config)
            for _ in range(config.n_layers)
        ])
        self.norm = RMSNorm(dim=config.dim)

        if self.peft_config.peft_mode == peft.PEFT_PROMPT:
            self.peft_prompt = peft.AddSoftPrompt(config=config, peft_config=peft_config)

    def forward(self,
                input_ids,
                attention_mask,
                cos, sin,
                kv_cache=None):
        """
        :param input_ids: [batch_size, seq_len]
        :param attention_mask: [batch_size=1, num_heads=1, seq_len, seq_len]
        :param kv_cache: See init_kv_cache.
        :param cos: for RoPE
        :param sin: for RoPE
        """
        hidden_states = self.embed_tokens(input_ids)
        if self.peft_config.peft_mode == peft.PEFT_PROMPT:
            if kv_cache is None or kv_cache[0]["key"].shape[2] == 0:
                # Only add prompt if kv_cache is None (full forward pass) or if kv_cache is empty (first decode step)
                hidden_states = self.peft_prompt(hidden_states)

        new_kv_cache = []
        for layer_i, layer in enumerate(self.layers):
            if kv_cache:
                # dict(
                #   key = [batch_size, num_heads, kv_seq_len=decode_step+1, head_dim]
                #   value = [batch_size, num_heads, kv_seq_len=decode_step+1, head_dim]
                # )
                layer_kv_cache = kv_cache[layer_i]
            else:
                layer_kv_cache = None

            if self.config.gradient_checkpointing:
                layer_out = torch.utils.checkpoint.checkpoint(
                    layer,
                    hidden_states,
                    attention_mask,
                    kv_cache,
                    cos, sin,
                )
            else:
                layer_out = layer(
                    hidden_states=hidden_states,
                    attention_mask=attention_mask,
                    kv_cache=layer_kv_cache,
                    cos=cos, sin=sin,
                )

            hidden_states = layer_out["hidden_states"]
            if kv_cache:
                new_kv_cache.append(layer_out["kv_cache"])
        hidden_states = self.norm(hidden_states)
        output = {
            "hidden_states": hidden_states
        }
        if kv_cache:
            output["kv_cache"] = new_kv_cache
        return output


class LLaMALayer(nn.Module):
    def __init__(self, config: LLaMAConfig, peft_config: peft.PeftConfig):
        super().__init__()
        self.config = config
        self.peft_config = peft_config
        self.self_attn = Attention(config=config, peft_config=peft_config)
        self.mlp = MLP(config=config, peft_config=peft_config)
        self.input_layernorm = RMSNorm(dim=config.dim, dtype=config.dtype)
        self.post_attention_layernorm = RMSNorm(dim=config.dim, dtype=config.dtype)

        if self.peft_config.peft_mode == peft.PEFT_ADAPTER:
            if self.peft_config.adapter_version == "houlsby":
                self.peft_adapter_attn = peft.Adapter(config=config, peft_config=peft_config)
            self.peft_adapter_mlp = peft.Adapter(config=config, peft_config=peft_config)

        if self.peft_config.peft_mode == peft.PEFT_BITFIT:
            self.peft_input_layernorm_bias = peft.BitFitAddBias(dim=config.dim, dtype=config.dtype)
            self.peft_post_attention_layernorm_bias = peft.BitFitAddBias(dim=config.dim, dtype=config.dtype)

    def forward(
        self,
        hidden_states,
        attention_mask,
        cos, sin,
        kv_cache=None,
    ):
        # 1) Self-attention
        # [batch_size, seq_len, hidden_dim]
        normed_hidden_states = self.input_layernorm(hidden_states)
        if self.peft_config.peft_mode == peft.PEFT_BITFIT:
            normed_hidden_states = self.peft_input_layernorm_bias(normed_hidden_states)
        # dict(
        #   attn_output = [batch_size, seq_len, hidden_dim]
        #   kv_cache = dict(
        #     key = [batch_size, num_heads, kv_seq_len, head_dim]
        #     value = [batch_size, num_heads, kv_seq_len, head_dim]
        #   )
        # )
        check_nan(normed_hidden_states)
        raw_self_attn_output = self.self_attn(
            hidden_states=normed_hidden_states,
            attention_mask=attention_mask,
            kv_cache=kv_cache,
            cos=cos, sin=sin,
        )
        # [batch_size, seq_len, hidden_dim]
        attn_out = raw_self_attn_output["attn_output"]
        if self.peft_config.peft_mode == peft.PEFT_ADAPTER \
                and self.peft_config.adapter_version == peft.ADAPTER_VERSION_HOULSBY:
            attn_out = self.peft_adapter_attn(attn_out)

        # [batch_size, seq_len, hidden_dim]
        hidden_states = hidden_states + attn_out
        check_nan(hidden_states)
        # 2) FFN
        # [batch_size, seq_len, hidden_dim]
        post_normed_hidden_states = self.post_attention_layernorm(hidden_states)
        if self.peft_config.peft_mode == peft.PEFT_BITFIT:
            post_normed_hidden_states = self.peft_post_attention_layernorm_bias(post_normed_hidden_states)

        mlp_out = self.mlp(post_normed_hidden_states)
        if self.peft_config.peft_mode == peft.PEFT_ADAPTER:
            mlp_out = self.peft_adapter_mlp(mlp_out)

        hidden_states = hidden_states + mlp_out
        check_nan(hidden_states)
        if kv_cache:
            return {
                "hidden_states": hidden_states,
                "kv_cache": raw_self_attn_output["kv_cache"],
            }

        return {"hidden_states": hidden_states}


class MLP(nn.Module):
    def __init__(
        self,
        config: LLaMAConfig,
        peft_config: peft.PeftConfig,
        multiple_of: int = 256,
    ):
        super().__init__()
        self.config = config
        self.peft_config = peft_config
        dim = config.dim
        hidden_dim = 4 * dim
        hidden_dim = int(2 * hidden_dim / 3)
        hidden_dim = multiple_of * ((hidden_dim + multiple_of - 1) // multiple_of)

        if config.use_8bit:
            self.gate_proj = NoInit8bitLinear(dim, hidden_dim, bias=False, threshold=6.0, has_fp16_weights=False)
            self.up_proj = NoInit8bitLinear(dim, hidden_dim, bias=False, threshold=6.0, has_fp16_weights=False)
            self.down_proj = NoInit8bitLinear(hidden_dim, dim, bias=False, threshold=6.0, has_fp16_weights=False)
        else:
            self.gate_proj = NoInitLinear(dim, hidden_dim, bias=False, dtype=config.dtype)
            self.up_proj = NoInitLinear(dim, hidden_dim, bias=False, dtype=config.dtype)
            self.down_proj = NoInitLinear(hidden_dim, dim, bias=False, dtype=config.dtype)

        if self.peft_config.peft_mode == peft.PEFT_IA3:
            self.peft_ia3 = peft.IA3ForMLP(config)
        if self.peft_config.peft_mode == peft.PEFT_BITFIT:
            self.peft_gate_proj_bias = peft.BitFitAddBias(dim=hidden_dim, dtype=config.dtype)
            self.peft_up_proj_bias = peft.BitFitAddBias(dim=hidden_dim, dtype=config.dtype)
            self.peft_down_proj_bias = peft.BitFitAddBias(dim=dim, dtype=config.dtype)

    def forward(self, x):
        gate_proj = self.gate_proj(x)
        up_proj = self.up_proj(x)
        if self.peft_config.peft_mode == peft.PEFT_BITFIT:
            gate_proj = self.peft_gate_proj_bias(gate_proj)
            up_proj = self.peft_gate_proj_bias(up_proj)

        intermediate_state = F.silu(gate_proj * up_proj)
        if self.peft_config.peft_mode == peft.PEFT_IA3:
            intermediate_state = self.peft_ia3(intermediate_state)

        down_proj = self.down_proj(intermediate_state)
        if self.peft_config.peft_mode == peft.PEFT_BITFIT:
            down_proj = self.peft_down_proj_bias(down_proj)

        return down_proj


class RMSNorm(torch.nn.Module):
    def __init__(self, dim: int, eps: float = 1e-6, dtype=torch.float16):
        super().__init__()
        self.eps = eps
        self.weight = nn.Parameter(torch.ones(dim, dtype=dtype))

    def _norm(self, x):
        return x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True) + self.eps)

    def forward(self, x):
        output = self._norm(x.float()).type_as(x)
        return output * self.weight


class Attention(nn.Module):
    def __init__(self, config: LLaMAConfig, peft_config: peft.PeftConfig):
        super().__init__()
        self.config = config
        self.peft_config = peft_config
        self.n_heads = config.n_heads
        self.head_dim = config.dim // config.n_heads

        if config.use_8bit:
            self.q_proj = NoInit8bitLinear(config.dim, config.dim, bias=False, threshold=6.0, has_fp16_weights=False)
            self.k_proj = NoInit8bitLinear(config.dim, config.dim, bias=False, threshold=6.0, has_fp16_weights=False)
            self.v_proj = NoInit8bitLinear(config.dim, config.dim, bias=False, threshold=6.0, has_fp16_weights=False)
            self.o_proj = NoInit8bitLinear(config.dim, config.dim, bias=False, threshold=6.0, has_fp16_weights=False)
        else:
            self.q_proj = NoInitLinear(config.dim, config.dim, bias=False, dtype=config.dtype)
            self.k_proj = NoInitLinear(config.dim, config.dim, bias=False, dtype=config.dtype)
            self.v_proj = NoInitLinear(config.dim, config.dim, bias=False, dtype=config.dtype)
            self.o_proj = NoInitLinear(config.dim, config.dim, bias=False, dtype=config.dtype)
        self.rotary_emb = RotaryEmbedding(dim=self.head_dim)

        if self.peft_config.peft_mode == peft.PEFT_LORA:
            self.peft_q_proj_lora = peft.LoRA(config=config, peft_config=peft_config)
            self.peft_v_proj_lora = peft.LoRA(config=config, peft_config=peft_config)
        if self.peft_config.peft_mode == peft.PEFT_IA3:
            self.peft_ia3 = peft.IA3ForAttn(config)
        if self.peft_config.peft_mode == peft.PEFT_BITFIT:
            self.peft_q_proj_bias = peft.BitFitAddBias(dim=config.dim, dtype=config.dtype)
            self.peft_k_proj_bias = peft.BitFitAddBias(dim=config.dim, dtype=config.dtype)
            self.peft_v_proj_bias = peft.BitFitAddBias(dim=config.dim, dtype=config.dtype)
            self.peft_o_proj_bias = peft.BitFitAddBias(dim=config.dim, dtype=config.dtype)
        if self.peft_config.peft_mode == peft.PEFT_PREFIX_ADAPTER:
            self.peft_prefix_adapter = peft.PrefixAdapter(config=config, peft_config=peft_config)

    def forward(self, hidden_states, attention_mask, cos, sin, kv_cache=None):
        """
        precomputed_kv_hidden_states is for init (pre-compute KV activations, e.g. for added prefixes)
        kv_cache is for generation (cached past KV)
        """
        batch_size, q_seq_len, hidden_dim = hidden_states.size()

        # (batch_size, num_heads, q_seq_len, head_dim)
        query_states = self.q_proj(hidden_states)
        key_states = self.k_proj(hidden_states)
        value_states = self.v_proj(hidden_states)

        if self.peft_config.peft_mode == peft.PEFT_LORA:
            query_states = self.peft_q_proj_lora(query_states)
            value_states = self.peft_v_proj_lora(value_states)
        if self.peft_config.peft_mode == peft.PEFT_IA3:
            key_states, value_states = self.peft_ia3(key_states, value_states)
        if self.peft_config.peft_mode == peft.PEFT_BITFIT:
            query_states = self.peft_q_proj_bias(query_states)
            key_states = self.peft_k_proj_bias(key_states)
            value_states = self.peft_v_proj_bias(value_states)

        query_states = query_states.view(
            batch_size, q_seq_len, self.n_heads, self.head_dim).transpose(1, 2)
        key_states = key_states.view(
            batch_size, q_seq_len, self.n_heads, self.head_dim).transpose(1, 2)
        value_states = value_states.view(
            batch_size, q_seq_len, self.n_heads, self.head_dim).transpose(1, 2)
        query_states, key_states = apply_rotary_pos_emb(query_states, key_states, cos=cos, sin=sin)

        if kv_cache:
            key_states = torch.cat([kv_cache["key"], key_states], dim=2)
            value_states = torch.cat([kv_cache["value"], value_states], dim=2)

        attn_output = torch.nn.functional.scaled_dot_product_attention(
            query=query_states,
            key=key_states,
            value=value_states,
            attn_mask=attention_mask,
        )

        if self.peft_config.peft_mode == peft.PEFT_PREFIX_ADAPTER:
            attn_output = attn_output + self.peft_prefix_adapter(query_states=query_states)

        # (batch_size, q_seq_len, hidden_dim)
        attn_output = attn_output.transpose(1, 2).contiguous().view(
            batch_size, q_seq_len, hidden_dim,
        )
        attn_output = self.o_proj(attn_output)
        if self.peft_config.peft_mode == peft.PEFT_BITFIT:
            attn_output = self.peft_o_proj_bias(attn_output)

        check_nan(attn_output)
        if kv_cache:
            new_kv_cache = {"key": key_states, "value": value_states}
            return {"attn_output": attn_output, "kv_cache": new_kv_cache}

        return {"attn_output": attn_output}


class RotaryEmbedding(torch.nn.Module):
    def __init__(self, dim, max_position_embeddings=2048, base=10000, device=None):
        super().__init__()
        inv_freq = 1.0 / (base ** (torch.arange(0, dim, 2).float().to(device=device) / dim))
        self.register_buffer("inv_freq", inv_freq)

        # Build here to make `torch.jit.trace` work.
        self.max_seq_len_cached = max_position_embeddings
        t = torch.arange(self.max_seq_len_cached, device=self.inv_freq.device).to(self.inv_freq.dtype)
        freqs = torch.einsum("i,j->ij", t, self.inv_freq)
        # Different from paper, but it uses a different permutation in order to obtain the same calculation
        emb = torch.cat((freqs, freqs), dim=-1)
        self.cos_cached = emb.cos()[None, None, :, :]
        self.sin_cached = emb.sin()[None, None, :, :]

    def forward(self, x, seq_len=None):
        # x: [bs, num_attention_heads, seq_len, head_size]
        # This `if` block is unlikely to be run after we build sin/cos in `__init__`. Keep the logic here just in case.
        if seq_len > self.max_seq_len_cached:
            self.max_seq_len_cached = seq_len
            t = torch.arange(self.max_seq_len_cached, device=x.device).to(self.inv_freq.dtype)
            freqs = torch.einsum("i,j->ij", t, self.inv_freq)
            # Different from paper, but it uses a different permutation in order to obtain the same calculation
            emb = torch.cat((freqs, freqs), dim=-1).to(x.device)
            self.cos_cached = emb.cos()[None, None, :, :].to(dtype=x.dtype)
            self.sin_cached = emb.sin()[None, None, :, :].to(dtype=x.dtype)
        return (
            self.cos_cached[:, :, :seq_len, ...].to(dtype=x.dtype, device=x.device),
            self.sin_cached[:, :, :seq_len, ...].to(dtype=x.dtype, device=x.device),
        )


def rotate_half(x):
    """Rotates half the hidden dims of the input."""
    x1 = x[..., : x.shape[-1] // 2]
    x2 = x[..., x.shape[-1] // 2:]
    return torch.cat((-x2, x1), dim=-1)


def apply_rotary_pos_emb(q, k, cos, sin):
    q_embed = (q * cos) + (rotate_half(q) * sin)
    k_embed = (k * cos) + (rotate_half(k) * sin)
    return q_embed, k_embed


def create_attention_mask(input_ids,
                          dtype=torch.float32,
                          return_soft_mask=True):
    """Create mask for decoder attention.

    Decoder masks have two use-cases:

    1) Training, where we see the full decoder sequence. In that case,
       we want a causal mask.

    2) Generation, where we only see one token at once. In that case,
       it doesn't really matter what we give, we can just give a 1.
       (i.e. seq_len = 1)

    Note that in both cases we do not care about which decoder_input_ids
    are valid, and also we can always simply broadcast over the batch size
    and heads.

    :param input_ids: [batch_size, seq_len]
    :param dtype: dtype
    :param return_soft_mask: whether to return mask or logits-mask
    :return: float [batch_size=1, num_heads=1, q_len=seq_len, kv_len=seq_len]
    """
    batch_size, seq_length = input_ids.shape
    # [seq_len]
    seq_ids = torch.arange(seq_length, device=input_ids.device)
    # [seq_len, seq_len]
    causal_mask = seq_ids[None, :].repeat(seq_length, 1) <= seq_ids[:, None]
    # [batch_size=1, num_heads=1, seq_len, seq_len]
    causal_mask = causal_mask[None, None, :, :]
    if return_soft_mask:
        return convert_mask_to_soft_mask(causal_mask, dtype=dtype)
    else:
        return causal_mask


def convert_mask_to_soft_mask(mask, dtype):
    """Convert binary mask to mask that can be added to logits.

    (i.e. 0 for attention, large negative for masked)
    """
    mask = mask.to(dtype=dtype)
    mask = (1.0 - mask) * torch.finfo(dtype).min
    return mask


class NoInitLinear(nn.Linear):
    def reset_parameters(self) -> None:
        pass


class NoInit8bitLinear(bnb.nn.Linear8bitLt):
    def reset_parameters(self) -> None:
        pass


def get_linear_class(use_8bit=False):
    if use_8bit:
        return NoInit8bitLinear
    else:
        return NoInitLinear


class NoInitEmbedding(nn.Embedding):
    def reset_parameters(self) -> None:
        pass


def check_nan(x):
    if torch.isnan(x).any():
        import pdb
        pdb.set_trace()


def create_model(model_name, hf_path, peft_config: peft.PeftConfig, use_8bit=False, device=None):
    config = LLAMA_CONFIG_DICT[model_name]

    with open(os.path.join(hf_path, "pytorch_model.bin.index.json")) as f:
        weight_map = json.load(f)["weight_map"]

    filename_list = sorted(list(set(weight_map.values())))
    if device is None:
        # TODO: Local rank
        device = torch.device("cuda:0")
    if use_8bit:
        config = dataclasses.replace(config, use_8bit=True)
        with init_empty_weights():
            model = LLaMAModel(config=config, peft_config=peft_config)
        state_keys = set(model.state_dict())
        filename_list = sorted(list(set(weight_map.values())))
        for filename in tqdm.tqdm(filename_list):
            loaded = torch.load(os.path.join(hf_path, filename), map_location="cpu")
            for k, v in loaded.items():
                set_module_8bit_tensor_to_device(model, tensor_name=k, device=device, value=v)
                state_keys.remove(k)
        assert not state_keys
    else:
        # noinspection PyUnresolvedReferences
        torch.set_default_tensor_type(torch.cuda.HalfTensor)
        model = LLaMAModel(config=config, peft_config=peft_config).cuda()
        torch.set_default_tensor_type(torch.FloatTensor)
        state_keys = set(model.state_dict())
        for filename in tqdm.tqdm(filename_list):
            loaded = torch.load(os.path.join(hf_path, filename), map_location="cpu")
            model.load_state_dict(loaded, strict=False)
            for k in loaded:
                state_keys.remove(k)
    return model


def shift_kv_cache_right(layer_cache, num_valid_tokens):
    """
    :param layer_cache: left-aligned kv cache element, [batch_size, num_heads, seq_len, dim]
    :param num_valid_tokens: [batch_size]
    :return:
    """
    batch_size = layer_cache.shape[0]
    # noinspection PyUnresolvedReferences
    return torch.stack([
        torch.cat([
            layer_cache[i, :, num_valid_tokens[i]:, :],
            layer_cache[i, :, :num_valid_tokens[i], :],
        ], dim=1)
        for i in range(batch_size)
    ], dim=0)


def create_generation_attention_mask(batch_size, seq_len, num_valid_tokens, device):
    """
    :param batch_size: int
    :param seq_len: int
    :param num_valid_tokens: [batch_size]
    :param device:
    :return:
    """
    # For right-aligned, based on num_valid_tokens
    # noinspection PyTypeChecker
    attn_mask = torch.zeros([batch_size, 1, 1, seq_len], dtype=bool)
    for i in range(batch_size):
        valid = num_valid_tokens[i]
        # noinspection PyTypeChecker
        # attn_mask[i, 0, -valid:, -valid:] = torch.tril(torch.ones([valid, valid], dtype=bool))
        attn_mask[i, 0, 0, -valid:] = True
    return attn_mask.to(device=device)


def create_casual_attention_mask(seq_len, device):
    # noinspection PyTypeChecker
    attn_mask = torch.tril(torch.ones([seq_len, seq_len], dtype=bool))[None, None, :, :]
    return attn_mask.to(device=device)


def create_rope_embed_ids(input_ids):
    pad_token_id = 0
    max_position = 2047  # These will not actually be used, as they are masked out by the attention mask
    x = (input_ids != pad_token_id).cumsum(-1) - 1
    x[input_ids == pad_token_id] = max_position
    return x


def zeros_like(shape, tensor):
    return torch.zeros(shape).type_as(tensor).to(tensor.device)
