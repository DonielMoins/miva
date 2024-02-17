import os
from shutil import copyfile
from typing import Any, Dict, List, Optional, Tuple, Union
import json
import tempfile
from functools import partial

import numpy as np
import jax
from jax.lib import xla_bridge
import jax.numpy as jnp
from jax import lax
from jax.sharding import PartitionSpec as PS
from jax.experimental.shard_map import shard_map
import flax.linen as nn
from flax.core.frozen_dict import FrozenDict, freeze, unfreeze
from flax.linen import combine_masks, make_causal_mask
from flax.traverse_util import flatten_dict, unflatten_dict
from flax.linen import partitioning as nn_partitioning

import sentencepiece as spm
from transformers.configuration_utils import PretrainedConfig
from transformers.utils import logging
from transformers.tokenization_utils import PreTrainedTokenizer
from transformers.modeling_flax_outputs import FlaxBaseModelOutput, FlaxCausalLMOutput
from transformers.modeling_flax_utils import FlaxPreTrainedModel
from transformers.utils import add_start_docstrings, add_start_docstrings_to_model_forward, logging

from ml_collections import ConfigDict
from tux import function_args_to_config, load_pickle, open_file,  with_sharding_constraint, get_jax_mesh, get_gradient_checkpoint_policy
from lwm.ring_attention import blockwise_ffn, ring_flash_attention_tpu, \
    ring_attention_standard, ring_attention


# Move torch functions in S6 to jax.numpy
from torch.nn import functional as F
from einops import rearrange, repeat


USE_MAMBA = 1
USE_TRANSFORMER = ~USE_MAMBA
DIFFERENT_H_STATES_RECURRENT_UPDATE_MECHANISM = 0

# Mamba hyperparameters
# User hyperparameters
d_model = 16
state_size = 1024  # Example state size
seq_len = 100  # Example sequence length
batch_size = 128  # Example batch size



LLAMA_STANDARD_CONFIGS = {
    '200m': {
        'vocab_size': 32000,
        'hidden_size': 1024,
        'intermediate_size': 2048,
        'num_hidden_layers': 14,
        'num_attention_heads': 8,
        'max_sequence_length': 2048,
        'initializer_range': 0.02,
        'rms_norm_eps': 1e-6,
        'use_cache': True,
        'tie_word_embeddings': False,
    },
    '1b': {
        'vocab_size': 32000,
        'hidden_size': 2048,
        'intermediate_size': 5504,
        'num_hidden_layers': 22,
        'num_attention_heads': 16,
        'max_sequence_length': 2048,
        'initializer_range': 0.02,
        'rms_norm_eps': 1e-6,
        'use_cache': True,
        'tie_word_embeddings': False,
    },
    '3b': {
        'vocab_size': 32000,
        'hidden_size': 3200,
        'intermediate_size': 8640,
        'num_hidden_layers': 26,
        'num_attention_heads': 32,
        'max_sequence_length': 2048,
        'initializer_range': 0.02,
        'rms_norm_eps': 1e-6,
        'use_cache': True,
        'tie_word_embeddings': False,
    },
    '7b': {
        'vocab_size': 32000,
        'hidden_size': 4096,
        'intermediate_size': 11008,
        'num_hidden_layers': 32,
        'num_attention_heads': 32,
        'max_sequence_length': 4096,
        'initializer_range': 0.02,
        'rms_norm_eps': 1e-6,
        'use_cache': True,
        'tie_word_embeddings': False,
    },
    '13b': {
        'vocab_size': 32000,
        'hidden_size': 5120,
        'intermediate_size': 13824,
        'num_hidden_layers': 40,
        'num_attention_heads': 40,
        'max_sequence_length': 2048,
        'initializer_range': 0.02,
        'rms_norm_eps': 1e-6,
        'use_cache': True,
        'tie_word_embeddings': False,
    },
    '30b': {
        'vocab_size': 32000,
        'hidden_size': 6656,
        'intermediate_size': 17920,
        'num_hidden_layers': 60,
        'num_attention_heads': 52,
        'max_sequence_length': 2048,
        'initializer_range': 0.02,
        'rms_norm_eps': 1e-6,
        'use_cache': True,
        'tie_word_embeddings': False,
    },
    '65b': {
        'vocab_size': 32000,
        'hidden_size': 8192,
        'intermediate_size': 22016,
        'num_hidden_layers': 80,
        'num_attention_heads': 64,
        'max_sequence_length': 2048,
        'initializer_range': 0.02,
        'rms_norm_eps': 1e-5,
        'use_cache': True,
        'tie_word_embeddings': False,
    },
    'debug': { # A small model for debugging
        'vocab_size': 32000,
        'hidden_size': 256,
        'intermediate_size': 256,
        'num_hidden_layers': 2,
        'num_attention_heads': 2,
        'max_sequence_length': 2048,
        'initializer_range': 0.02,
        'rms_norm_eps': 1e-6,
        'use_cache': True,
        'tie_word_embeddings': False,
    },
}


class LLaMAConfig(PretrainedConfig):
    r"""
    This is the configuration class to store the configuration of a [`~LLaMAModel`]. It is used to instantiate an LLaMA
    model according to the specified arguments, defining the model architecture. Instantiating a configuration with the
    defaults will yield a similar configuration to that of the LLaMA-7B.
    Configuration objects inherit from [`PretrainedConfig`] and can be used to control the model outputs. Read the
    documentation from [`PretrainedConfig`] for more information.
    Args:
        vocab_size (`int`, *optional*, defaults to 32000):
            Vocabulary size of the LLaMA model. Defines the number of different tokens that can be represented by the
            `inputs_ids` passed when calling [`~LLaMAModel`] or [`~TFLLaMAModel`].
        hidden_size (`int`, *optional*, defaults to 4096):
            Dimension of the hidden representations.
        intermediate_size (`int`, *optional*, defaults to 11008):
            Dimension of the MLP representations.
        num_hidden_layers (`int`, *optional*, defaults to 32):
            Number of hidden layers in the Transformer encoder.
        num_attention_heads (`int`, *optional*, defaults to 32):
            Number of attention heads for each attention layer in the Transformer encoder.
        hidden_act (`str` or `function`, *optional*, defaults to `"silu"`):
            The non-linear activation function (function or string) in the decoder.
        max_sequence_length (`int`, *optional*, defaults to 2048):
            Max sequence length for model (for RoPE computation)
        initializer_range (`float`, *optional*, defaults to 0.02):
            The standard deviation of the truncated_normal_initializer for initializing all weight matrices.
        rms_norm_eps (`float`, *optional*, defaults to 1e-12):
            The epsilon used by the rms normalization layers.
        use_cache (`bool`, *optional*, defaults to `True`):
            Whether or not the model should return the last key/values attentions (not used by all models). Only
            relevant if `config.is_decoder=True`.
        tie_word_embeddings(`bool`, *optional*, defaults to `False`):
            Whether to tie weight embeddings
        Example:
    ```python
    >>> from transformers import LLaMAModel, LLaMAConfig
    >>> # Initializing a LLaMA llama-7b style configuration
    >>> configuration = LLaMAConfig()
    >>> # Initializing a model from the llama-7b style configuration
    >>> model = LLaMAModel(configuration)
    >>> # Accessing the model configuration
    >>> configuration = model.config
    ```"""
    model_type = "llama"

    def __init__(
        self,
        vocab_size=32000,
        hidden_size=4096,
        intermediate_size=11008,
        num_hidden_layers=32,
        num_attention_heads=32,
        max_sequence_length=4096,
        orig_sequence_length=4096,
        rms_norm_eps=1e-6,
        initializer_range=0.02,
        use_cache=True,
        bos_token_id=0,
        eos_token_id=1,
        resid_pdrop=0.0,
        embd_pdrop=0.0,
        attn_pdrop=0.0,
        tie_word_embeddings=False,
        remat_block='',
        remat_attention='',
        remat_mlp='',
        scan_attention=False,
        scan_mlp=False,
        scan_query_chunk_size=1024,
        scan_key_chunk_size=1024,
        scan_mlp_chunk_size=1024,
        fcm_min_ratio=0.0,
        fcm_max_ratio=0.0,
        scan_layers=True,
        param_scan_axis=0,
        mesh_dim=None,
        use_flash_attention=True,
        theta=10000,
        **kwargs,
    ):
        self.vocab_size = vocab_size
        self.hidden_size = hidden_size
        self.initializer_range = initializer_range
        self.intermediate_size = intermediate_size
        self.num_hidden_layers = num_hidden_layers
        self.num_attention_heads = num_attention_heads
        self.max_sequence_length = max_sequence_length
        self.orig_sequence_length = orig_sequence_length
        self.rms_norm_eps = rms_norm_eps
        self.use_cache = use_cache
        self.resid_pdrop = resid_pdrop
        self.embd_pdrop = embd_pdrop
        self.attn_pdrop = attn_pdrop
        self.remat_block = remat_block
        self.remat_attention = remat_attention
        self.remat_mlp = remat_mlp
        self.scan_attention = scan_attention
        self.scan_mlp = scan_mlp
        self.scan_query_chunk_size = scan_query_chunk_size
        self.scan_key_chunk_size = scan_key_chunk_size
        self.scan_mlp_chunk_size = scan_mlp_chunk_size
        self.fcm_min_ratio = fcm_min_ratio
        self.fcm_max_ratio = fcm_max_ratio
        self.scan_layers = scan_layers
        self.param_scan_axis = param_scan_axis
        self.mesh_dim = mesh_dim
        self.use_flash_attention = use_flash_attention
        self.theta = theta
        super().__init__(
            bos_token_id=bos_token_id,
            eos_token_id=eos_token_id,
            tie_word_embeddings=tie_word_embeddings,
            **kwargs,
        )

    @classmethod
    def get_default_config(cls, updates=None):
        config = function_args_to_config(cls.__init__)

        if updates is not None:
            config.update(ConfigDict(updates).copy_and_resolve_references())

        return config

    @staticmethod
    def get_jax_mesh(axis_dims):
        return get_jax_mesh(axis_dims, ('dp', 'fsdp', 'tp', 'sp'))

    @staticmethod
    def get_ranks_and_size(mesh):
        out = dict(mesh=mesh)
        mp_size = mesh.shape['tp'] * mesh.shape['sp']
        mp_node_size = max(1, mp_size // jax.local_device_count())
        dp_node_size = jax.process_count() // mp_node_size
        out.update(mp_node_size=mp_node_size,
                   dp_node_size=dp_node_size)

        dp_node_rank = jax.process_index() // mp_node_size
        mp_node_rank = jax.process_index() % mp_node_size
        out.update(dp_node_rank=dp_node_rank,
                   mp_node_rank=mp_node_rank)
        return out


    @staticmethod
    def get_partition_rules(scan_layers=False, scan_axis=0):
        """ Parition rules for GPTJ. Note that these rules are orderd, so that
            the beginning rules match first. It is important to use
            PartitionSpec() instead of None here because JAX does not treat
            None as a pytree leaf.
        """
        if scan_layers:
            if scan_axis == 0:
                return (
                    # embeddings
                    ("transformer/wte/embedding", PS("tp", ("fsdp", "sp"))),
                    # atention
                    ("attention/(wq|wk|wv)/kernel", PS(None, ("fsdp", "sp"), "tp")),
                    ("attention/wo/kernel", PS(None, "tp", ("fsdp", "sp"))),
                    # mlp
                    ("feed_forward/w1/kernel", PS(None, ("fsdp", "sp"), "tp")),
                    ("feed_forward/w2/kernel", PS(None, "tp", ("fsdp", "sp"))),
                    ("feed_forward/w3/kernel", PS(None, ("fsdp", "sp"), "tp")),
                    # layer norms
                    ("attention_norm/kernel", PS(None, None)),
                    ("ffn_norm/kernel", PS(None, None)),
                    # output head
                    ("transformer/ln_f/kernel", PS(None)),
                    ("lm_head/kernel", PS(("fsdp", "sp"), "tp")),
                    ('.*', PS(None)),
                )
            elif scan_axis == 1:
                return (
                    # embeddings
                    ("transformer/wte/embedding", PS("tp", ("fsdp", "sp"))),
                    # atention
                    ("attention/(wq|wk|wv)/kernel", PS(("fsdp", "sp"), None, "tp")),
                    ("attention/wo/kernel", PS("tp", None, ("fsdp", "sp"))),
                    # mlp
                    ("feed_forward/w1/kernel", PS(("fsdp", "sp"), None, "tp")),
                    ("feed_forward/w2/kernel", PS("tp", None, ("fsdp", "sp"))),
                    ("feed_forward/w3/kernel", PS(("fsdp", "sp"), None, "tp")),
                    # layer norms
                    ("attention_norm/kernel", PS(None, None)),
                    ("ffn_norm/kernel", PS(None, None)),
                    # output head
                    ("transformer/ln_f/kernel", PS(None)),
                    ("lm_head/kernel", PS(("fsdp", "sp"), "tp")),
                    ('.*', PS(None)),
                )
            else:
                raise ValueError(f"Invalid scan_axis {scan_axis}")
        else:
            return (
                # embeddings
                ("transformer/wte/embedding", PS("tp", ("fsdp", "sp"))),
                # atention
                ("attention/(wq|wk|wv)/kernel", PS(("fsdp", "sp"), "tp")),
                ("attention/wo/kernel", PS("tp", ("fsdp", "sp"))),
                # mlp
                ("feed_forward/w1/kernel", PS(("fsdp", "sp"), "tp")),
                ("feed_forward/w2/kernel", PS("tp", ("fsdp", "sp"))),
                ("feed_forward/w3/kernel", PS(("fsdp", "sp"), "tp")),
                # layer norms
                ("attention_norm/kernel", PS(None)),
                ("ffn_norm/kernel", PS(None)),
                # output head
                ("transformer/ln_f/kernel", PS(None)),
                ("lm_head/kernel", PS(("fsdp", "sp"), "tp")),
                ('.*', PS(None)),
            )

    @staticmethod
    def get_weight_decay_exclusions():
        return tuple()
        
    @staticmethod
    def get_frozen_param_exclusions(freeze_base):
        if freeze_base:
            return ("vte", "vision_head")
        else:
            return tuple()

    @staticmethod
    def rng_keys():
        return ('params', 'dropout', 'fcm')

    @staticmethod
    def get_tokenizer_config(updates=None):
        config = ConfigDict()
        config.vocab_file = ''
        config.add_bos_token = False
        config.add_eos_token = False

        if updates is not None:
            config.update(ConfigDict(updates).copy_and_resolve_references())
        return config

    @classmethod
    def get_tokenizer(cls, config, padding_side='left', truncation_side='right'):
        config = cls.get_tokenizer_config(config)
        assert config.vocab_file != '', 'vocab_file must be specified'
        tokenizer = LLaMATokenizer(
            vocab_file=config.vocab_file,
            add_bos_token=config.add_bos_token,
            add_eos_token=config.add_eos_token,
            padding_side=padding_side,
            truncation_side=truncation_side,
        )
        return tokenizer

    @classmethod
    def load_config(cls, path):
        if path in LLAMA_STANDARD_CONFIGS:
            return cls.from_dict(LLAMA_STANDARD_CONFIGS[path])
        load_type, load_path = path.split('::', 1)
        if load_type == 'pickle':
            return cls.from_dict(load_pickle(load_path)['llama_config'])
        elif load_type == 'json':
            with open_file(load_path, 'r') as fin:
                raw_config = fin.read()
            return cls.from_dict(json.loads(raw_config))
        else:
            raise ValueError(f'Unsupported load config type: {load_type}')


remat = nn_partitioning.remat

logger = logging.get_logger(__name__)


class RMSNorm(nn.Module):
    dim: int
    eps: float=1e-6
    dtype: jnp.dtype=jnp.float32
    param_dtype: jnp.dtype=jnp.float32

    def setup(self) -> None:
        self.weight = self.param(
            'kernel',
            nn.initializers.ones,
            (self.dim,),
            self.param_dtype,
        )

    def _norm(self, x: jnp.ndarray) -> jnp.ndarray:
        return x * jax.lax.rsqrt(jnp.square(x).mean(-1, keepdims=True) + self.eps)

    def __call__(self, x: jnp.ndarray) -> jnp.ndarray:
        x = x.astype(jnp.promote_types(self.dtype, jnp.float32))
        output = self._norm(x).astype(self.dtype)
        weight = jnp.asarray(self.weight, self.dtype)
        return output * weight


def precompute_freqs_cis(dim: int, max_position_embedding: int, theta: float=10000.0, dtype: jnp.dtype=jnp.float32) -> jnp.ndarray:
    freqs = 1.0 / (theta ** (np.arange(0, dim, 2)[: (dim // 2)].astype(dtype) / dim))
    t = np.arange(max_position_embedding) # type: ignore
    freqs = np.outer(t, freqs).astype(dtype)  # type: ignore
    sin, cos = np.sin(freqs), np.cos(freqs)
    freqs_cis = np.complex64(cos + 1j * sin)
    return jnp.asarray(freqs_cis)


def apply_rotary_emb(
    xq: jnp.ndarray,
    xk: jnp.ndarray,
    freqs_cis: jnp.ndarray,
    dtype: jnp.dtype=jnp.float32,
) -> Tuple[jnp.ndarray, jnp.ndarray]:

    reshape_xq = xq.astype(jnp.float32).reshape(*xq.shape[:-1], -1, 2)
    reshape_xk = xk.astype(jnp.float32).reshape(*xk.shape[:-1], -1, 2)

    xq_ = jax.lax.complex(reshape_xq[..., 0], reshape_xq[..., 1])
    xk_ = jax.lax.complex(reshape_xk[..., 0], reshape_xk[..., 1])

    # add head dim
    freqs_cis = jnp.reshape(freqs_cis, (*freqs_cis.shape[:2], 1, *freqs_cis.shape[2:]))

    xq_out = xq_ * freqs_cis
    xq_out = jnp.stack((jnp.real(xq_out), jnp.imag(xq_out)), axis=-1).reshape(*xq_out.shape[:-1], -1)

    xk_out = xk_ * freqs_cis
    xk_out = jnp.stack((jnp.real(xk_out), jnp.imag(xk_out)), axis=-1).reshape(*xk_out.shape[:-1], -1)

    return xq_out.astype(dtype), xk_out.astype(dtype)


class FlaxLLaMAAttention(nn.Module):
    config: LLaMAConfig
    dtype: jnp.dtype=jnp.float32
    param_dtype: jnp.dtype=jnp.float32
    precision: Optional[Union[jax.lax.Precision, str]]=None

    def setup(self):
        config = self.config
        self.embed_dim = config.hidden_size
        self.num_heads = config.num_attention_heads
        self.head_dim = self.embed_dim // self.num_heads

        self.wq = nn.Dense(
            config.num_attention_heads*self.head_dim,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            use_bias=False,
            kernel_init=jax.nn.initializers.normal(self.config.initializer_range),
            precision=self.precision,
        )
        self.wk = nn.Dense(
            config.num_attention_heads*self.head_dim,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            use_bias=False,
            kernel_init=jax.nn.initializers.normal(self.config.initializer_range),
            precision=self.precision,
        )
        self.wv = nn.Dense(
            config.num_attention_heads*self.head_dim,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            use_bias=False,
            kernel_init=jax.nn.initializers.normal(self.config.initializer_range),
            precision=self.precision,
        )
        self.wo = nn.Dense(
            config.hidden_size,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            use_bias=False,
            kernel_init=jax.nn.initializers.normal(self.config.initializer_range),
            precision=self.precision,
        )

        self.resid_dropout = nn.Dropout(rate=config.resid_pdrop)

        self.causal_mask = make_causal_mask(jnp.ones((1, config.max_sequence_length), dtype="bool"), dtype="bool")

        self.freqs_cis = precompute_freqs_cis(
            self.head_dim,
            config.max_sequence_length,
            theta=config.theta,
            dtype=self.dtype,
        )

    def _split_heads(self, hidden_states):
        return hidden_states.reshape(hidden_states.shape[:2] + (self.num_heads, self.head_dim))

    def _merge_heads(self, hidden_states):
        return hidden_states.reshape(hidden_states.shape[:2] + (self.embed_dim,))

    @nn.compact
    def _concatenate_to_cache(self, key, value, query, attention_mask):
        """
        This function takes projected key, value states from a single input token and concatenates the states to cached
        states from previous steps. This function is slighly adapted from the official Flax repository:
        https://github.com/google/flax/blob/491ce18759622506588784b4fca0e4bf05f8c8cd/flax/linen/attention.py#L252
        """
        # detect if we're initializing by absence of existing cache data.
        is_initialized = self.has_variable("cache", "cached_key")
        cached_key = self.variable("cache", "cached_key", jnp.zeros, key.shape, key.dtype)
        cached_value = self.variable("cache", "cached_value", jnp.zeros, value.shape, value.dtype)
        cache_index = self.variable("cache", "cache_index", lambda: jnp.array(0, dtype=jnp.int32))

        if is_initialized:
            *batch_dims, max_length, num_heads, depth_per_head = cached_key.value.shape
            # update key, value caches with our new 1d spatial slices
            cur_index = cache_index.value
            if query.shape[1] == 1:
                mesh = LLaMAConfig.get_jax_mesh(self.config.mesh_dim)
                def fn(cached_key, cached_value, key, value, cur_index):
                    assert key.shape[1] == 1 and value.shape[1] == 1, (key.shape, value.shape)
                    sp_size = max_length // mesh.shape['sp']
                    axis_index = jax.lax.axis_index('sp')
                    cur_index = cur_index - axis_index * sp_size
                    key, value = jax.lax.cond(
                        jnp.logical_and(cur_index >= 0, cur_index < sp_size),
                        lambda: (
                            cached_key.at[:, cur_index].set(key[:, -1]),
                            cached_value.at[:, cur_index].set(value[:, -1]),
                        ),
                        lambda: (cached_key, cached_value),
                    )
                    return key, value
                fn = shard_map(
                    fn, mesh=mesh,
                    in_specs=(
                        PS(('dp', 'fsdp'), 'sp', 'tp', None), 
                        PS(('dp', 'fsdp'), 'sp', 'tp', None), 
                        PS(('dp', 'fsdp'), None, 'tp', None), 
                        PS(('dp', 'fsdp'), None, 'tp', None),
                        PS()
                    ),
                    out_specs=(
                        PS(('dp', 'fsdp'), 'sp', 'tp', None),
                        PS(('dp', 'fsdp'), 'sp', 'tp', None)
                    ),
                    check_rep=False
                )
                key, value = fn(cached_key.value, cached_value.value, key, value, cur_index)
            else:
                indices = (0,) * len(batch_dims) + (cur_index, 0, 0)
                key = lax.dynamic_update_slice(cached_key.value, key, indices)
                value = lax.dynamic_update_slice(cached_value.value, value, indices)
            cached_key.value = key
            cached_value.value = value
            num_updated_cache_vectors = query.shape[1]
            cache_index.value = cache_index.value + num_updated_cache_vectors
        return key, value, attention_mask

    def __call__(
        self,
        hidden_states,
        attention_mask,
        segment_ids,
        position_ids,
        deterministic: bool = True,
        init_cache: bool = False,
        output_attentions: bool = False,
        fcm_mask=None,
    ):
        xq, xk, xv = self.wq(hidden_states), self.wk(hidden_states), self.wv(hidden_states)

        if xq.shape[1] == 1:
            xq = with_sharding_constraint(xq, PS(("dp", "fsdp"), None, "tp"))
        else:
            xq = with_sharding_constraint(xq, PS(("dp", "fsdp"), "sp", "tp"))
        xk = with_sharding_constraint(xk, PS(("dp", "fsdp"), "sp", "tp"))
        xv = with_sharding_constraint(xv, PS(("dp", "fsdp"), "sp", "tp"))

        xq = self._split_heads(xq)
        xk = self._split_heads(xk)
        xv = self._split_heads(xv)

        freqs_cis = jnp.take(self.freqs_cis, position_ids, axis=0)

        xq, xk = apply_rotary_emb(xq, xk, freqs_cis=freqs_cis, dtype=self.dtype)

        dropout_rng = None
        if not deterministic and self.config.attn_pdrop > 0.0:
            dropout_rng = self.make_rng("dropout")

        if self.config.scan_attention and xq.shape[1] > max(self.config.scan_query_chunk_size, self.config.scan_key_chunk_size):
            # attention mask without nxn materlization, blockwise_attn will handle the rest
            attention_mask = jnp.expand_dims(attention_mask, axis=(-3, -2))

            if self.has_variable("cache", "cached_key") or init_cache:
                xk, xv, attention_mask = self._concatenate_to_cache(xk, xv, xq, attention_mask)

            # transform boolean mask into float mask
            attention_bias = lax.select(
                attention_mask > 0,
                jnp.full(attention_mask.shape, 0.0).astype(self.dtype),
                jnp.full(attention_mask.shape, jnp.finfo(self.dtype).min).astype(self.dtype),
            )
            attn_weights = None

            platform = xla_bridge.get_backend().platform
            if self.config.use_flash_attention and platform == "tpu":
                ring_attention_fn = ring_flash_attention_tpu
            else:
                ring_attention_fn = ring_attention # uses BPT attention
            ring_attention_sharded = shard_map(
                partial(
                    ring_attention_fn,
                    axis_name="sp",
                    float32_logits=True,
                    blockwise_kwargs=dict(
                        deterministic=deterministic,
                        dropout_rng=dropout_rng,
                        attn_pdrop=self.config.attn_pdrop,
                        causal=True,
                        query_chunk_size=self.config.scan_query_chunk_size,
                        key_chunk_size=self.config.scan_key_chunk_size,
                        dtype=self.dtype,
                        policy=get_gradient_checkpoint_policy('nothing_saveable'),
                        precision=self.precision,
                        prevent_cse=not self.config.scan_layers,
                    )
                ),
                mesh=LLaMAConfig.get_jax_mesh(self.config.mesh_dim),
                in_specs=(
                    PS(("dp", "fsdp"), "sp", "tp", None),
                    PS(("dp", "fsdp"), "sp", "tp", None),
                    PS(("dp", "fsdp"), "sp", "tp", None),
                    PS(("dp", "fsdp"), None, None, None),
                    PS(("dp", "fsdp"), None), 
                ),
                out_specs=PS(("dp", "fsdp"), "sp", "tp", None),
                check_rep=False
            )
            attn_output = ring_attention_sharded(xq, xk, xv, attention_bias, segment_ids)
            attn_output = with_sharding_constraint(attn_output, PS(("dp", "fsdp"), "sp", "tp", None))
        else:
            query_length, key_length = xq.shape[1], xk.shape[1]

            if self.has_variable("cache", "cached_key"):
                mask_shift = self.variables["cache"]["cache_index"]
                max_decoder_length = self.variables["cache"]["cached_key"].shape[1]
                causal_mask = jnp.arange(max_decoder_length)[None] <= (jnp.arange(query_length) + mask_shift)[:, None]
                causal_mask = causal_mask[None, None]
                segment_mask = None
            else:
                causal_mask = self.causal_mask[:, :, :query_length, :key_length]
                segment_mask = segment_ids[:, :, None] == segment_ids[:, None, :]
                segment_mask = segment_mask[:, None]

            batch_size = hidden_states.shape[0]
            causal_mask = jnp.broadcast_to(causal_mask, (batch_size,) + causal_mask.shape[1:])

            attention_mask = jnp.broadcast_to(jnp.expand_dims(attention_mask, axis=(-3, -2)), causal_mask.shape)
            attention_mask = combine_masks(attention_mask, causal_mask, fcm_mask, segment_mask)

            # During fast autoregressive decoding, we feed one position at a time,
            # and cache the keys and values step by step.
            if self.has_variable("cache", "cached_key") or init_cache:
                xk, xv, attention_mask = self._concatenate_to_cache(xk, xv, xq, attention_mask)

            q_sp_dim = None if xq.shape[1] == 1 else 'sp'
            attn_weights = None
            ring_attention_sharded = shard_map(
                partial(ring_attention_standard, axis_name="sp"), mesh=LLaMAConfig.get_jax_mesh(self.config.mesh_dim),
                in_specs=(
                    PS(("dp", "fsdp"), q_sp_dim, "tp", None),
                    PS(("dp", "fsdp"), "sp", "tp", None),
                    PS(("dp", "fsdp"), "sp", "tp", None),
                    PS(("dp", "fsdp"), None, q_sp_dim, None)
                ),
                out_specs=PS(("dp", "fsdp"), q_sp_dim, "tp", None),
                check_rep=False
            )
            attn_output = ring_attention_sharded(
                xq, xk, xv, attention_mask
            )

        attn_output = self._merge_heads(attn_output)
        attn_output = self.wo(attn_output)
        attn_output = self.resid_dropout(attn_output, deterministic=deterministic)
        outputs = (attn_output, attn_weights) if output_attentions else (attn_output,)
        return outputs

# TODO: Convert nn calls to flax equivalent
class S6(nn.Module):
    def __init__(self, seq_len, d_model, state_size, device):
        super(S6, self).__init__()

        self.fc1 = nn.Linear(d_model, d_model, device=device)
        self.fc2 = nn.Linear(d_model, state_size, device=device)
        self.fc3 = nn.Linear(d_model, state_size, device=device)

        self.seq_len = seq_len
        self.d_model = d_model
        self.state_size = state_size

        #self.A = nn.Parameter(torch.ones(d_model, state_size, device=device))
        #self.A = nn.Parameter(F.normalize(torch.ones(d_model, state_size, device=device), p=2, dim=-1))
        #nn.init.xavier_uniform_(self.A)

        # S4D real initialization, MAMBA removed imaginary portions for S4D-Inv and S4D-Lin initialization schemes
        # described in [On the Parameterization and Initialization of Diagonal State Space Models](https://arxiv.org/abs/2206.11893)
        # https://github.com/state-spaces/mamba/blob/fb7b5310fa865dbd62aa059b1e26f2b431363e2a/mamba_ssm/modules/mamba_simple.py#L103-L108C23
        A = repeat(
            nn.arange(1, state_size + 1, dtype=nn.float32, device=device),
            "n -> d n",
            d=d_model,
        ).contiguous()

        A_log = nn.log(A)  # For numerical stability during training process
        self.A_log = nn.Parameter(A_log)
        self.A_log._no_weight_decay = True

        self.A = nn.zeros_like(self.A_log)
        self.B = nn.zeros(batch_size, self.seq_len, self.state_size, device=device)
        self.C = nn.zeros(batch_size, self.seq_len, self.state_size, device=device)

        #self.delta = torch.zeros(batch_size, self.seq_len, self.d_model, device=device)
        # Initialize delta parameter using a uniform distribution and apply the inverse softplus
        uniform_distribution = nn.distributions.Uniform(0.001, 0.1)
        # Sample from the uniform distribution and then apply the inverse softplus
        self.delta = self.inverse_softplus(uniform_distribution.sample((batch_size, self.seq_len, self.d_model)))

        self.dA = nn.zeros(batch_size, self.seq_len, self.d_model, self.state_size, device=device)
        self.dB = nn.zeros(batch_size, self.seq_len, self.d_model, self.state_size, device=device)

        # h should have dimensions [batch_size, seq_len, d_model, state_size]
        self.h = nn.zeros(batch_size, self.seq_len, self.d_model, self.state_size, device=device)
        self.y = nn.zeros(batch_size, self.seq_len, self.d_model, device=device)


    def inverse_softplus(self, y):
        return nn.log(nn.exp(y) - 1)

    def discretization(self):
        # discretization function is defined based on the MAMBA paper's description using ZOH on page 28
        # in Section C : Mechanics on Selective SSMs
        # See also "Zero-order hold discretization" maths proof inside https://studywolf.wordpress.com/tag/zero-order-hold/
        """
        Here is an explanation of the mathematical rationale for the formulation of Δt used in Mamba:
        The key idea is that Δt controls the discretization rate of the continuous SSM dynamics. By making Δt input-dependent, it introduces selectivity into the discrete transition matrices.
        Specifically, in Mamba they parameterize Δt as:
        Δt = τΔ(Parameter + sΔ(xt))
        Where:
        - Parameter is a learned scalar parameter that controls the baseline discretization rate
        - sΔ(xt) is a projection that makes Δt input-dependent by computing a value based on xt
        - τΔ(x) = softplus(x) transforms the result to be positive through the softplus nonlinearity
        The rationale for this formulation is:
        - Parameter provides a reasonable default discretization rate
        - sΔ(xt) injects input-dependence through the projection
        - softplus ensures Δt is positive as required to be a valid timestep
        - The projection sΔ allows the model to learn to modulate Δt based on the input xt
        - This modulation creates selectivity in how rapidly or slowly the states update
        So in summary, the learned input-dependent projection allows Δt, and thus the discrete dynamics, to become selective. The softplus and scalar parameter provide useful inductive biases on top of this flexibility.
        The end result is discrete transition matrices that are selective on the input, enabling powerful sequence modeling capabilities.
        Credit: Claude2 AI chatbot
        """

        # For numerical stability during training process
        self.A = -nn.exp(self.A_log.float())  # (d_model, state_size)

        #print(f"self.A.shape = {self.A.shape}")
        #print(f"self.B.shape = {self.B.shape}")
        #print(f"self.delta.shape = {self.delta.shape}")

        # inverse() only supports square matrix
        #dB = torch.matmul(torch.inverse(A * delta), torch.matmul(dA - torch.eye(A.shape[0]), B))
        self.dB = nn.einsum("bld,bln->bldn", self.delta, self.B)

        # https://github.com/state-spaces/mamba/blob/0131c1e94a46fc9f70bcfc9d57962963bb2f0b9e/mamba_ssm/modules/mamba_simple.py#L240
        #dA = torch.matrix_exp(A * delta)  # matrix_exp() only supports square matrix
        self.dA = nn.exp(nn.einsum("bld,dn->bldn", self.delta, self.A))
        #print(f"self.dA.shape = {self.dA.shape}")
        #print(f"self.dA.requires_grad = {self.dA.requires_grad}")

        return self.dA, self.dB

    def forward(self, x):
        # Refer to Algorithm 2 in the MAMBA paper
        self.B = self.fc2(x)
        self.C = self.fc3(x)

        # "a large ∆ resets the state `h` and focuses on the current input `x`,
        # while a small ∆ persists the state and ignores the current input."
        self.delta = F.softplus(self.fc1(x))

        # Uses ZOH as in MAMBA, Hungry Hippo still uses bilinear transform for discretization
        self.discretization()

        if DIFFERENT_H_STATES_RECURRENT_UPDATE_MECHANISM:  # this will trigger in-place runtime error if without using `h_new`
            #print(f"self.dA = {self.dA}, self.dB = {self.dB}")
            #print(f"self.dA.shape = {self.dA.shape}")
            #print(f"self.dB.shape = {self.dB.shape}")
            #print(f"x.shape = {x.shape}")
            #print(f"self.h.shape = {self.h.shape}")
            #print(f"self.C.shape = {self.C.shape}")

            global current_batch_size
            current_batch_size = x.shape[0]

            if self.h.shape[0] != current_batch_size:
                #print("Adjusting h_new for the different batch size of input data `x`")
                different_batch_size = True

                # Resize self.h to match the current batch size
                h_new =  nn.einsum('bldn,bldn->bldn', self.dA, self.h[:current_batch_size, ...]) + rearrange(x, "b l d -> b l d 1") * self.dB

            else:
                different_batch_size = False
                h_new =  nn.einsum('bldn,bldn->bldn', self.dA, self.h) + rearrange(x, "b l d -> b l d 1") * self.dB

            # y needs to have a shape of [batch_size, seq_len, d_model]
            self.y = nn.einsum('bln,bldn->bld', self.C, h_new)

            # Update self.h with the detached state of h_new
            # Only do this if retaining gradients for self.h is not necessary for backprop
            # Otherwise, store h_new in a temporary list and update self.h after the loop
            global temp_buffer
            temp_buffer = h_new.detach().clone() if not self.h.requires_grad else h_new.clone()
            #print(f"temp_buffer.shape = {temp_buffer.shape}")

            #print(f"self.y = {self.y}")
            #print(f"self.dA.requires_grad = {self.dA.requires_grad}")
            #print(f"self.dB.requires_grad = {self.dB.requires_grad}")
            #print(f"self.C.requires_grad = {self.C.requires_grad}")
            #print(f"self.h.requires_grad = {self.h.requires_grad}")
            #print(f"self.y.requires_grad = {self.y.requires_grad}")

            return self.y

        else:  # this will not trigger in-place runtime error
            # h should have dimensions [batch_size, seq_len, d_model, state_size]
            h = nn.zeros(x.size(0), self.seq_len, self.d_model, self.state_size, device=x.device)
            y = nn.zeros_like(x)

            h =  nn.einsum('bldn,bldn->bldn', self.dA, h) + rearrange(x, "b l d -> b l d 1") * self.dB

            # y needs to have a shape of [batch_size, seq_len, d_model]
            y = nn.einsum('bln,bldn->bld', self.C, h)

            return y

class FlaxLLaMAMLP(nn.Module):
    config: LLaMAConfig
    dtype: jnp.dtype=jnp.float32
    param_dtype: jnp.dtype=jnp.float32
    precision: Optional[Union[jax.lax.Precision, str]]=None

    def setup(self) -> None:
        config = self.config
        self.w1 = nn.Dense(
            config.intermediate_size,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            use_bias=False,
            kernel_init=jax.nn.initializers.normal(self.config.initializer_range),
            precision=self.precision,
        )
        self.w2 = nn.Dense(
            config.hidden_size,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            use_bias=False,
            kernel_init=jax.nn.initializers.normal(self.config.initializer_range),
            precision=self.precision,
        )
        self.w3 = nn.Dense(
            config.intermediate_size,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            use_bias=False,
            kernel_init=jax.nn.initializers.normal(self.config.initializer_range),
            precision=self.precision,
        )
        self.dropout = nn.Dropout(rate=self.config.resid_pdrop)

    def __call__(self, x: jnp.ndarray, deterministic: bool = True) -> jnp.ndarray:
        x = self.w2(nn.silu(self.w1(x)) * self.w3(x))
        x = self.dropout(x, deterministic=deterministic)
        return x

class MambaBlock(nn.Module):
    def __init__(self, seq_len, d_model, state_size, device):
        super(MambaBlock, self).__init__()

        self.inp_proj = nn.Linear(d_model, 2*d_model, device=device)
        self.out_proj = nn.Linear(2*d_model, d_model, device=device)

        # For residual skip connection
        self.D = nn.Linear(d_model, 2*d_model, device=device)

        # Set _no_weight_decay attribute on bias
        self.out_proj.bias._no_weight_decay = True

        # Initialize bias to a small constant value
        nn.init.constant_(self.out_proj.bias, 1.0)

        self.S6 = S6(seq_len, 2*d_model, state_size, device)

        # Add 1D convolution with kernel size 3
        self.conv = nn.Conv1d(seq_len, seq_len, kernel_size=3, padding=1, device=device)

        # rmsnorm
        self.norm = RMSNorm(d_model, device=device)


    def forward(self, x, attention_mask=None):

        if attention_mask is not None:
            # Apply the attention mask
            x = x * attention_mask.unsqueeze(-1)

        """
        x_proj.shape = torch.Size([batch_size, seq_len, 2*d_model])
        x_conv.shape = torch.Size([batch_size, seq_len, 2*d_model])
        x_conv_act.shape = torch.Size([batch_size, seq_len, 2*d_model])
        """
        # Refer to Figure 3 in the MAMBA paper

        x = self.norm(x)

        x_proj = self.inp_proj(x)
        #print(f"x_proj.shape = {x_proj.shape}")

        # Add 1D convolution with kernel size 3
        x_conv = self.conv(x_proj)

        # Create a triangular mask of the same shape as the input sequence
        mask = nn.tril(nn.ones(seq_len, 2*d_model, device=device))

        # Add batch dimension with unsqueeze(0) -> (1, seq_len, seq_len)
        # Repeat batch dim to match x_conv batches with .repeat()
        current_batch_size = x.shape[0]
        mask = mask.repeat(current_batch_size, 1, 1)

        # Apply causal mask to zero out the masked regions
        x_conv = x_conv * mask
        #print(f"x_conv.shape = {x_conv.shape}")

        x_conv_act = F.silu(x_conv)  # Swish activation can be implemented as x * sigmoid(x)
        #print(f"x_conv_act.shape = {x_conv_act.shape}")

        x_ssm = self.S6(x_conv_act)
        #print(f"x_ssm.shape = {x_ssm.shape}")

        # residual skip connection with nonlinearity introduced by multiplication
        x_residual = F.silu(self.D(x))
        #print(f"x_residual.shape = {x_residual.shape}")
        x_combined = x_ssm * x_residual
        #print(f"x_combined.shape = {x_combined.shape}")

        x_out = self.out_proj(x_combined)
        #print(f"x_out.shape = {x_out.shape}")

        return x_out



class FlaxLLaMABlock(nn.Module):
    config: LLaMAConfig
    dtype: jnp.dtype=jnp.float32
    param_dtype: jnp.dtype=jnp.float32
    precision: Optional[Union[jax.lax.Precision, str]]=None

    def setup(self) -> None:
        attention_module = FlaxLLaMAAttention
        mlp_module = FlaxLLaMAMLP
        if self.config.remat_attention != '':
            attention_module = remat(
                FlaxLLaMAAttention, static_argnums=(4, 5, 6),
                policy=get_gradient_checkpoint_policy(self.config.remat_attention),
                prevent_cse=not self.config.scan_layers,
            )
        if self.config.remat_mlp != '':
            mlp_module = remat(
                FlaxLLaMAMLP, static_argnums=(1,),
                policy=get_gradient_checkpoint_policy(self.config.remat_mlp),
                prevent_cse=not self.config.scan_layers,
            )

        self.attention = attention_module(
            self.config,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision,
        )
        self.feed_forward = mlp_module(
            self.config,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision,
        )
        self.attention_norm = RMSNorm(
            self.config.hidden_size,
            eps=self.config.rms_norm_eps,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
        )
        self.ffn_norm = RMSNorm(
            self.config.hidden_size,
            eps=self.config.rms_norm_eps,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
        )
        

    def __call__(
        self,
        hidden_states,
        attention_mask=None,
        segment_ids=None,
        position_ids=None,
        deterministic: bool = True,
        init_cache: bool = False,
        output_attentions: bool = False,
        fcm_mask: Optional[jnp.ndarray] = None,
    ):
        attn_outputs = self.attention(
            self.attention_norm(hidden_states),
            attention_mask,
            segment_ids,
            position_ids,
            deterministic,
            init_cache,
            output_attentions,
            fcm_mask,
        )
        attn_output = attn_outputs[0]
        hidden_states = hidden_states + attn_output

        feed_forward_input = self.ffn_norm(hidden_states)

        if self.config.scan_mlp and hidden_states.shape[1] >= self.config.scan_mlp_chunk_size:
            feed_forward_hidden_states = blockwise_ffn(
                self.feed_forward,
                feed_forward_input,
                self.config.scan_mlp_chunk_size,
                deterministic,
            )
        else:
            feed_forward_hidden_states = self.feed_forward(
                feed_forward_input,
                deterministic,
            )
        feed_forward_hidden_states = with_sharding_constraint(feed_forward_hidden_states, PS(("dp", "fsdp"), None, "tp"))

        hidden_states = hidden_states + feed_forward_hidden_states

        # return (hidden_states,) + attn_outputs[1:]
        outputs = hidden_states
        if self.config.scan_layers:
            outputs = (outputs, None)
        return outputs


class FlaxLLaMAPreTrainedModel(FlaxPreTrainedModel):
    """
    An abstract class to handle weights initialization and a simple interface for downloading and loading pretrained
    models.
    """

    config_class = LLaMAConfig
    base_model_prefix = "transformer"
    module_class: nn.Module = None

    def __init__(
        self,
        config: LLaMAConfig,
        input_shape: Tuple = (1, 1),
        seed: int = 0,
        dtype: jnp.dtype = jnp.float32,
        _do_init: bool = True,
        **kwargs,
    ):
        module = self.module_class(config=config, dtype=dtype, **kwargs)
        super().__init__(config, module, input_shape=input_shape, seed=seed, dtype=dtype, _do_init=_do_init)

    def init_weights(self, rng: jax.random.PRNGKey, input_shape: Tuple, params: FrozenDict = None) -> FrozenDict:
        # init input tensors
        input_ids = jnp.zeros(input_shape, dtype="i4")
        attention_mask = jnp.ones_like(input_ids)
        segment_ids = jnp.zeros_like(input_ids)
        position_ids = jnp.broadcast_to(jnp.arange(jnp.atleast_2d(input_ids).shape[-1]), input_shape)
        params_rng, dropout_rng = jax.random.split(rng)
        rngs = {"params": params_rng, "dropout": dropout_rng}

        if self.config.add_cross_attention:
            encoder_hidden_states = jnp.zeros(input_shape + (self.config.hidden_size,))
            encoder_attention_mask = attention_mask
            module_init_outputs = self.module.init(
                rngs,
                input_ids,
                attention_mask,
                segment_ids,
                position_ids,
                encoder_hidden_states,
                encoder_attention_mask,
                return_dict=False,
            )
        else:
            module_init_outputs = self.module.init(rngs, input_ids, attention_mask, position_ids, return_dict=False)

        random_params = module_init_outputs["params"]

        if params is not None:
            random_params = flatten_dict(unfreeze(random_params))
            params = flatten_dict(unfreeze(params))
            for missing_key in self._missing_keys:
                params[missing_key] = random_params[missing_key]
            self._missing_keys = set()
            return freeze(unflatten_dict(params))
        else:
            return random_params

    def init_cache(self, batch_size, max_length):
        r"""
        Args:
            batch_size (`int`):
                batch_size used for fast auto-regressive decoding. Defines the batch size of the initialized cache.
            max_length (`int`):
                maximum possible length for auto-regressive decoding. Defines the sequence length of the initialized
                cache.
        """
        # init input variables to retrieve cache
        input_ids = jnp.ones((batch_size, max_length))
        attention_mask = jnp.ones_like(input_ids)
        segment_ids = jnp.zeros_like(input_ids)
        position_ids = jnp.broadcast_to(jnp.arange(jnp.atleast_2d(input_ids).shape[-1]), input_ids.shape)

        init_variables = self.module.init(
            jax.random.PRNGKey(0), input_ids, attention_mask, segment_ids, position_ids, return_dict=False, init_cache=True
        )
        return init_variables["cache"].unfreeze()

    @add_start_docstrings_to_model_forward("")
    def __call__(
        self,
        input_ids,
        attention_mask=None,
        segment_ids=None,
        position_ids=None,
        params: dict = None,
        past_key_values: dict = None,
        dropout_rng: jax.random.PRNGKey = None,
        train: bool = False,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ):
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.return_dict

        batch_size, sequence_length = input_ids.shape

        if position_ids is None:
            if past_key_values is not None:
                raise ValueError("Make sure to provide `position_ids` when passing `past_key_values`.")

            position_ids = jnp.broadcast_to(jnp.arange(sequence_length)[None, :], (batch_size, sequence_length))

        if attention_mask is None:
            attention_mask = jnp.ones((batch_size, sequence_length))
        if segment_ids is None:
            segment_ids = jnp.zeros((batch_size, sequence_length))

        # Handle any PRNG if needed
        rngs = {}
        if dropout_rng is not None:
            rngs["dropout"] = dropout_rng

        inputs = {"params": params or self.params}

        # if past_key_values are passed then cache is already initialized a private flag init_cache has to be passed down to ensure cache is used. It has to be made sure that cache is marked as mutable so that it can be changed by FlaxGPTJAttention module
        if past_key_values:
            inputs["cache"] = past_key_values
            mutable = ["cache"]
        else:
            mutable = False

        outputs = self.module.apply(
            inputs,
            jnp.array(input_ids, dtype="i4"),
            jnp.array(attention_mask, dtype="i4"),
            jnp.array(segment_ids, dtype="i4"),
            jnp.array(position_ids, dtype="i4"),
            not train,
            False,
            output_attentions,
            output_hidden_states,
            return_dict,
            rngs=rngs,
            mutable=mutable,
        )

        # add updated cache to model output
        if past_key_values is not None and return_dict:
            outputs, past_key_values = outputs
            outputs["past_key_values"] = unfreeze(past_key_values["cache"])
            return outputs
        elif past_key_values is not None and not return_dict:
            outputs, past_key_values = outputs
            outputs = outputs[:1] + (unfreeze(past_key_values["cache"]),) + outputs[1:]

        return outputs


class FlaxLLaMABlockCollection(nn.Module):
    config: LLaMAConfig
    dtype: jnp.dtype = jnp.float32
    param_dtype: jnp.dtype=jnp.float32
    precision: Optional[Union[jax.lax.Precision, str]]=None

    @nn.compact
    def __call__(
        self,
        hidden_states,
        attention_mask=None,
        segment_ids=None,
        position_ids=None,
        deterministic: bool = True,
        init_cache: bool = False,
        output_attentions: bool = False,
        output_hidden_states: bool = False,
        return_dict: bool = True,
    ):
        all_attentions = () if output_attentions else None
        all_hidden_states = () if output_hidden_states else None

        if not deterministic and self.config.fcm_max_ratio > 0:
            # Apply forgetful causal mask
            batch_size, seq_length = hidden_states.shape[0], hidden_states.shape[1]
            fcm_ratio = jax.random.uniform(
                self.make_rng('fcm'), shape=(batch_size, 1, 1, 1),
                minval=self.config.fcm_min_ratio,
                maxval=self.config.fcm_max_ratio
            )
            fcm_mask = jax.random.uniform(
                self.make_rng('fcm'),
                shape=(batch_size, 1, seq_length, seq_length)
            ) > fcm_ratio
            fcm_mask = fcm_mask.at[:, :, :, 0].set(True)
            fcm_mask = fcm_mask.astype('bool')
        else:
            fcm_mask = None

        block = FlaxLLaMABlock
        if self.config.remat_block != '':
            block = remat(
                FlaxLLaMABlock, static_argnums=(4, 5, 6),
                prevent_cse=not self.config.scan_layers,
                policy=get_gradient_checkpoint_policy(self.config.remat_block)
            )
        if self.config.scan_layers:
            initializing = self.is_mutable_collection('params')
            params_spec = (
                self.config.param_scan_axis if initializing else
                nn_partitioning.ScanIn(self.config.param_scan_axis))
            cache_spec = 0
            hidden_states, _ = nn.scan(
                block,
                variable_axes={
                    'params': params_spec,
                    'cache': cache_spec,
                    'intermediates': 0
                },
                split_rngs={
                    'params': True,
                    'dropout': True
                },
                in_axes=(nn.broadcast, nn.broadcast, nn.broadcast, nn.broadcast, nn.broadcast, nn.broadcast, nn.broadcast),
                length=self.config.num_hidden_layers,
                metadata_params={nn.PARTITION_NAME: 'scan_decoder_layer'},
                )(self.config, name='scan_decoder', dtype=self.dtype, param_dtype=self.param_dtype,)(
                    hidden_states,
                    attention_mask,
                    segment_ids,
                    position_ids,
                    deterministic,
                    init_cache,
                    output_attentions,
                    fcm_mask,
                )
        else:
            blocks = [
                block(
                    self.config,
                    name=str(i),
                    dtype=self.dtype,
                    param_dtype=self.param_dtype,
                ) for i in range(self.config.num_hidden_layers)
            ]
            for block in blocks:
                if output_hidden_states:
                    all_hidden_states += (hidden_states,)

                layer_outputs = block(
                    hidden_states,
                    attention_mask,
                    segment_ids,
                    position_ids,
                    deterministic,
                    init_cache,
                    output_attentions,
                    fcm_mask,
                )
                hidden_states = layer_outputs

                if output_attentions:
                    all_attentions += (layer_outputs[1],)

        # this contains possible `None` values - `FlaxGPTJModule` will filter them out
        outputs = (hidden_states, all_hidden_states, all_attentions)

        return outputs


class FlaxLLaMAModule(nn.Module):
    config: LLaMAConfig
    dtype: jnp.dtype = jnp.float32
    param_dtype: jnp.dtype=jnp.float32
    precision: Optional[Union[jax.lax.Precision, str]]=None

    def setup(self):
        self.embed_dim = self.config.hidden_size

        self.wte = nn.Embed(
            self.config.vocab_size,
            self.config.hidden_size,
            embedding_init=jax.nn.initializers.normal(stddev=self.config.initializer_range),
            dtype=self.dtype,
            param_dtype=self.param_dtype,
        )
        self.dropout = nn.Dropout(rate=self.config.embd_pdrop)
        self.h = FlaxLLaMABlockCollection(self.config, dtype=self.dtype, param_dtype=self.param_dtype, precision=self.precision)
        
        self.m1 = MambaBlock(self.config.max_sequence_length, self.config.hidden_size, self.config.scan_mlp_chunk_size, "cuda")
        self.m2 = MambaBlock(self.config.max_sequence_length, self.config.hidden_size, self.config.scan_mlp_chunk_size, "cuda")
        self.m3 = MambaBlock(self.config.max_sequence_length, self.config.hidden_size, self.config.scan_mlp_chunk_size, "cuda")
        self.final_proj = nn.Linear(self.config.hidden_size, self.config.hidden_size, device=device)

        self.ln_f = RMSNorm(self.config.hidden_size, eps=self.config.rms_norm_eps, dtype=self.dtype, param_dtype=self.param_dtype)

    def __call__(
        self,
        input_ids,
        attention_mask,
        segment_ids,
        position_ids,
        deterministic=True,
        init_cache: bool = False,
        output_attentions: bool = False,
        output_hidden_states: bool = False,
        return_dict: bool = True,
    ):
        input_embeds = self.wte(input_ids.astype("i4"))

        hidden_states = self.dropout(input_embeds, deterministic=deterministic)

        outputs = self.h(
            hidden_states,
            attention_mask,
            segment_ids=segment_ids,
            position_ids=position_ids,
            deterministic=deterministic,
            init_cache=init_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        hidden_states = outputs[0]
        
        # Mamba blocks
        hidden_states = self.m1(hidden_states)
        hidden_states = self.m2(hidden_states)
        hidden_states = self.m3(hidden_states)
        
        hidden_states = self.final_proj(hidden_states)
        
        hidden_states = self.ln_f(hidden_states)

        if output_hidden_states:
            all_hidden_states = outputs[1] + (hidden_states,)
            outputs = (hidden_states, all_hidden_states) + outputs[2:]
        else:
            outputs = (hidden_states,) + outputs[1:]

        if not return_dict:
            return tuple(v for v in outputs if v is not None)

        return FlaxBaseModelOutput(
            last_hidden_state=hidden_states,
            hidden_states=outputs[1],
            attentions=outputs[-1],
        )

@add_start_docstrings("", "")
class FlaxLLaMAModel(FlaxLLaMAPreTrainedModel):
    module_class = FlaxLLaMAModule

class FlaxLLaMAForCausalLMModule(nn.Module):
    config: LLaMAConfig
    dtype: jnp.dtype = jnp.float32
    param_dtype: jnp.dtype=jnp.float32
    precision: Optional[Union[jax.lax.Precision, str]]=None

    def setup(self):
        self.transmamba = FlaxLLaMAModule(self.config, dtype=self.dtype)
        self.lm_head = nn.Dense(
            self.config.vocab_size,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            use_bias=False,
            kernel_init=jax.nn.initializers.normal(stddev=self.config.initializer_range),
            precision=self.precision,
        )

    def __call__(
        self,
        input_ids,
        attention_mask=None,
        segment_ids=None,
        position_ids=None,
        deterministic: bool = True,
        init_cache: bool = False,
        output_attentions: bool = False,
        output_hidden_states: bool = False,
        return_dict: bool = True,
    ):
        batch_size, seq_length = input_ids.shape
        if attention_mask is None:
            attention_mask = jnp.ones_like(input_ids)
        if segment_ids is None:
            segment_ids = jnp.zeros_like(input_ids)
        if position_ids is None:
            position_ids = jnp.broadcast_to(
                jnp.clip(jnp.cumsum(attention_mask, axis=-1) - 1, a_min=0),
                (batch_size, seq_length)
            )
        outputs = self.transmamba(
            input_ids,
            attention_mask,
            segment_ids,
            position_ids,
            deterministic=deterministic,
            init_cache=init_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        hidden_states = outputs[0]

        if self.config.tie_word_embeddings:
            shared_kernel = self.transmamba.variables["params"]["wte"]["embedding"].T
            lm_logits = self.lm_head.apply({"params": {"kernel": shared_kernel}}, hidden_states)
        else:
            lm_logits = self.lm_head(hidden_states)

        if not return_dict:
            return (lm_logits,) + outputs[1:]

        return FlaxCausalLMOutput(logits=lm_logits, hidden_states=outputs.hidden_states, attentions=outputs.attentions)


@add_start_docstrings("", "")
class FlaxLLaMAForCausalLM(FlaxLLaMAPreTrainedModel):
    module_class = FlaxLLaMAForCausalLMModule

    def prepare_inputs_for_generation(
        self, input_ids, max_length, 
        attention_mask: Optional[jax.Array] = None, 
    ):
        # initializing the cache
        batch_size, seq_length = input_ids.shape

        past_key_values = self.init_cache(batch_size, max_length)
        # Note that usually one would have to put 0's in the attention_mask for x > input_ids.shape[-1] and x < cache_length.
        # But since GPTJ uses a causal mask, those positions are masked anyways.
        # Thus we can create a single static attention_mask here, which is more efficient for compilation
        extended_attention_mask = jnp.ones((batch_size, max_length), dtype="i4")
        if attention_mask is not None:
            position_ids = attention_mask.cumsum(axis=-1) - 1
            extended_attention_mask = lax.dynamic_update_slice(extended_attention_mask, attention_mask, (0, 0))
        else:
            position_ids = jnp.broadcast_to(jnp.arange(seq_length, dtype="i4")[None, :], (batch_size, seq_length))

        return {
            "past_key_values": past_key_values,
            "attention_mask": extended_attention_mask,
            "position_ids": position_ids,
        }

    def update_inputs_for_generation(self, model_outputs, model_kwargs):
        model_kwargs["past_key_values"] = model_outputs.past_key_values
        model_kwargs["position_ids"] = model_kwargs["position_ids"][:, -1:] + 1
        return model_kwargs


VOCAB_FILES_NAMES = {"vocab_file": "tokenizer.model"}

PRETRAINED_VOCAB_FILES_MAP = {}


class LLaMATokenizer(PreTrainedTokenizer):
    """
    Construct a LLaMA tokenizer. Based on byte-level Byte-Pair-Encoding.
    Args:
        vocab_file (`str`):
            Path to the vocabulary file.
    """

    vocab_files_names = VOCAB_FILES_NAMES
    pretrained_vocab_files_map = PRETRAINED_VOCAB_FILES_MAP
    model_input_names = ["input_ids", "attention_mask"]

    def __init__(
        self,
        vocab_file,
        unk_token="<unk>",
        bos_token="<s>",
        eos_token="</s>",
        sp_model_kwargs: Optional[Dict[str, Any]] = None,
        add_bos_token=False,
        add_eos_token=False,
        **kwargs,
    ):
        self.sp_model_kwargs = {} if sp_model_kwargs is None else sp_model_kwargs
        super().__init__(bos_token=bos_token, eos_token=eos_token, unk_token=unk_token, **kwargs)
        self.vocab_file = vocab_file
        self.add_bos_token = add_bos_token
        self.add_eos_token = add_eos_token
        self.sp_model = spm.SentencePieceProcessor(**self.sp_model_kwargs)

        with tempfile.NamedTemporaryFile() as tfile:
            with open_file(self.vocab_file, 'rb') as fin:
                tfile.write(fin.read())
                tfile.flush()
                tfile.seek(0)
            self.sp_model.Load(tfile.name)
        """ Initialisation"""
        self.add_special_tokens(dict(
            unk_token=unk_token,
            bos_token=bos_token,
            eos_token=eos_token,
        ))
        self.pad_token_id = self.unk_token_id

    @property
    def vocab_size(self):
        """Returns vocab size"""
        return self.sp_model.get_piece_size()

    @property
    def bos_token_id(self) -> Optional[int]:
        return self.sp_model.bos_id()

    @property
    def eos_token_id(self) -> Optional[int]:
        return self.sp_model.eos_id()

    def get_vocab(self):
        """Returns vocab as a dict"""
        vocab = {self.convert_ids_to_tokens(i): i for i in range(self.vocab_size)}
        vocab.update(self.added_tokens_encoder)
        return vocab

    def _tokenize(self, text):
        """Returns a tokenized string."""
        return self.sp_model.encode(text, out_type=str)

    def _convert_token_to_id(self, token):
        """Converts a token (str) in an id using the vocab."""
        return self.sp_model.piece_to_id(token)

    def _convert_id_to_token(self, index):
        """Converts an index (integer) in a token (str) using the vocab."""
        token = self.sp_model.IdToPiece(index)
        return token

    def convert_tokens_to_string(self, tokens):
        """Converts a sequence of tokens (string) in a single string."""
        current_sub_tokens = []
        out_string = ""
        prev_is_special = False
        for token in tokens:
            # make sure that special tokens are not decoded using sentencepiece model
            if token in self.all_special_tokens:
                if not prev_is_special:
                    out_string += " "
                out_string += self.sp_model.decode(current_sub_tokens) + token
                prev_is_special = True
                current_sub_tokens = []
            else:
                current_sub_tokens.append(token)
                prev_is_special = False
        out_string += self.sp_model.decode(current_sub_tokens)
        return out_string.strip()

    def save_vocabulary(self, save_directory, filename_prefix: Optional[str] = None) -> Tuple[str]:
        """
        Save the vocabulary and special tokens file to a directory.
        Args:
            save_directory (`str`):
                The directory in which to save the vocabulary.
        Returns:
            `Tuple(str)`: Paths to the files saved.
        """
        if not os.path.isdir(save_directory):
            logger.error(f"Vocabulary path ({save_directory}) should be a directory")
            return
        out_vocab_file = os.path.join(
            save_directory, (filename_prefix + "-" if filename_prefix else "") + VOCAB_FILES_NAMES["vocab_file"]
        )

        if os.path.abspath(self.vocab_file) != os.path.abspath(out_vocab_file) and os.path.isfile(self.vocab_file):
            copyfile(self.vocab_file, out_vocab_file)
        elif not os.path.isfile(self.vocab_file):
            with open(out_vocab_file, "wb") as fi:
                content_spiece_model = self.sp_model.serialized_model_proto()
                fi.write(content_spiece_model)

        return (out_vocab_file,)

    def build_inputs_with_special_tokens(self, token_ids_0, token_ids_1=None):
        if self.add_bos_token:
            bos_token_ids = [self.bos_token_id]
        else:
            bos_token_ids = []

        output = bos_token_ids + token_ids_0

        if token_ids_1 is not None:
            output = output + token_ids_1

        if self.add_eos_token:
            output = output + [self.eos_token_id]

        return output

    def get_special_tokens_mask(
        self, token_ids_0: List[int], token_ids_1: Optional[List[int]] = None, already_has_special_tokens: bool = False
    ) -> List[int]:
        """
        Retrieve sequence ids from a token list that has no special tokens added. This method is called when adding
        special tokens using the tokenizer `prepare_for_model` method.
        Args:
            token_ids_0 (`List[int]`):
                List of IDs.
            token_ids_1 (`List[int]`, *optional*):
                Optional second list of IDs for sequence pairs.
            already_has_special_tokens (`bool`, *optional*, defaults to `False`):
                Whether or not the token list is already formatted with special tokens for the model.
        Returns:
            `List[int]`: A list of integers in the range [0, 1]: 1 for a special token, 0 for a sequence token.
        """
        if already_has_special_tokens:
            return super().get_special_tokens_mask(
                token_ids_0=token_ids_0, token_ids_1=token_ids_1, already_has_special_tokens=True
            )

        if token_ids_1 is None:
            return [1] + ([0] * len(token_ids_0)) + [1]
        return [1] + ([0] * len(token_ids_0)) + [1, 1] + ([0] * len(token_ids_1)) + [1]

    def create_token_type_ids_from_sequences(
        self, token_ids_0: List[int], token_ids_1: Optional[List[int]] = None
    ) -> List[int]:
        """
        Create a mask from the two sequences passed to be used in a sequence-pair classification task. T5 does not make
        use of token type ids, therefore a list of zeros is returned.
        Args:
            token_ids_0 (`List[int]`):
                List of IDs.
            token_ids_1 (`List[int]`, *optional*):
                Optional second list of IDs for sequence pairs.
        Returns:
            `List[int]`: List of zeros.
        """
        eos = [self.eos_token_id]

        if token_ids_1 is None:
            return len(token_ids_0 + eos) * [0]
        return len(token_ids_0 + eos + token_ids_1 + eos) * [0]