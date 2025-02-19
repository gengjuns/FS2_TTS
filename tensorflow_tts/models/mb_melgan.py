# -*- coding: utf-8 -*-
# Copyright 2020 The Multi-band MelGAN Authors , Minh Nguyen (@dathudeptrai) and Tomoki Hayashi (@kan-bayashi)
#
"""Multi-band MelGAN Modules."""

import numpy as np
import tensorflow as tf
from scipy.signal import kaiser
from tensorflow_tts.modules import TFReflectionPad1d, TFResidualStack, TFConvTranspose1d

def get_initializer(initializer_seed=42):
    return tf.keras.initializers.GlorotNormal(seed=initializer_seed)

def design_prototype_filter(taps=62, cutoff_ratio=0.15, beta=9.0):
    """Design prototype filter for PQMF.
    This method is based on `A Kaiser window approach for the design of prototype
    filters of cosine modulated filterbanks`_.
    Args:
        taps (int): The number of filter taps.
        cutoff_ratio (float): Cut-off frequency ratio.
        beta (float): Beta coefficient for kaiser window.
    Returns:
        ndarray: Impluse response of prototype filter (taps + 1,).
    .. _`A Kaiser window approach for the design of prototype filters of cosine modulated filterbanks`:
        https://ieeexplore.ieee.org/abstract/document/681427
    """
    # check the arguments are valid
    assert taps % 2 == 0, "The number of taps mush be even number."
    assert 0.0 < cutoff_ratio < 1.0, "Cutoff ratio must be > 0.0 and < 1.0."

    # make initial filter
    omega_c = np.pi * cutoff_ratio
    with np.errstate(invalid="ignore"):
        h_i = np.sin(omega_c * (np.arange(taps + 1) - 0.5 * taps)) / (
            np.pi * (np.arange(taps + 1) - 0.5 * taps)
        )
    # fix nan due to indeterminate form
    h_i[taps // 2] = np.cos(0) * cutoff_ratio

    # apply kaiser window
    w = kaiser(taps + 1, beta)
    h = h_i * w

    return h

class MBMelGANGenerator(tf.keras.Model):
    """Tensorflow MBMelGAN generator module."""

    def __init__(self, config, **kwargs):
        super().__init__(config, **kwargs)
        assert config.filters >= np.prod(config.upsample_scales)
        assert config.filters % (2 ** len(config.upsample_scales)) == 0

        # add initial layer
        layers = []
        layers += [
            TFReflectionPad1d(
                (config.kernel_size - 1) // 2,
                padding_type=config.padding_type,
                name="first_reflect_padding",
            ),
            tf.keras.layers.Conv1D(
                filters=config.filters,
                kernel_size=config.kernel_size,
                use_bias=config.use_bias,
                kernel_initializer=get_initializer(config.initializer_seed),
            ),
        ]

        for i, upsample_scale in enumerate(config.upsample_scales):
            # add upsampling layer
            layers += [
                getattr(tf.keras.layers, config.nonlinear_activation)(
                    **config.nonlinear_activation_params
                ),
                TFConvTranspose1d(
                    filters=config.filters // (2 ** (i + 1)),
                    kernel_size=upsample_scale * 2,
                    strides=upsample_scale,
                    padding="same",
                    is_weight_norm=config.is_weight_norm,
                    initializer_seed=config.initializer_seed,
                    name="conv_transpose_._{}".format(i),
                ),
            ]

            # ad residual stack layer
            for j in range(config.stacks):
                layers += [
                    TFResidualStack(
                        kernel_size=config.stack_kernel_size,
                        filters=config.filters // (2 ** (i + 1)),
                        dilation_rate=config.stack_kernel_size ** j,
                        use_bias=config.use_bias,
                        nonlinear_activation=config.nonlinear_activation,
                        nonlinear_activation_params=config.nonlinear_activation_params,
                        is_weight_norm=config.is_weight_norm,
                        initializer_seed=config.initializer_seed,
                        name="residual_stack_._{}._._{}".format(i, j),
                    )
                ]
        # add final layer
        layers += [
            getattr(tf.keras.layers, config.nonlinear_activation)(
                **config.nonlinear_activation_params
            ),
            TFReflectionPad1d(
                (config.kernel_size - 1) // 2,
                padding_type=config.padding_type,
                name="last_reflect_padding",
            ),
            tf.keras.layers.Conv1D(
                filters=config.out_channels,
                kernel_size=config.kernel_size,
                use_bias=config.use_bias,
                kernel_initializer=get_initializer(config.initializer_seed),
            ),
        ]
        if config.use_final_nolinear_activation:
            layers += [tf.keras.layers.Activation("tanh")]

        self.melgan = tf.keras.models.Sequential(layers)
        self.pqmf = TFPQMF(config=config, name="pqmf")

    def call(self, mels, **kwargs):
        """Calculate forward propagation.
        Args:
            c (Tensor): Input tensor (B, T, channels)
        Returns:
            Tensor: Output tensor (B, T ** prod(upsample_scales), out_channels)
        """
        return self.inference(mels)

    @tf.function(
        input_signature=[
            tf.TensorSpec(shape=[None, None, 80], dtype=tf.float32, name="mels")
        ]
    )
    def inference(self, mels):
        mb_audios = self.melgan(mels)
        return self.pqmf.synthesis(mb_audios)

    @tf.function(
        input_signature=[
            tf.TensorSpec(shape=[1, None, 80], dtype=tf.float32, name="mels")
        ]
    )
    def inference_tflite(self, mels):
        mb_audios = self.melgan(mels)
        return self.pqmf.synthesis(mb_audios)

    def _build(self):
        """Build model by passing fake input."""
        fake_mels = tf.random.uniform(shape=[1, 100, 80], dtype=tf.float32)
        self(fake_mels)

class TFPQMF(tf.keras.layers.Layer):
    """PQMF module."""
    def __init__(self, config, **kwargs):
        """Initilize PQMF module.
        Args:
            config (class): MultiBandMelGANGeneratorConfig
        """
        super().__init__(**kwargs)
        subbands = config.subbands
        taps = config.taps
        cutoff_ratio = config.cutoff_ratio
        beta = config.beta

        # define filter coefficient
        h_proto = design_prototype_filter(taps, cutoff_ratio, beta)
        h_analysis = np.zeros((subbands, len(h_proto)))
        h_synthesis = np.zeros((subbands, len(h_proto)))
        for k in range(subbands):
            h_analysis[k] = (
                2
                * h_proto
                * np.cos(
                    (2 * k + 1)
                    * (np.pi / (2 * subbands))
                    * (np.arange(taps + 1) - (taps / 2))
                    + (-1) ** k * np.pi / 4
                )
            )
            h_synthesis[k] = (
                2
                * h_proto
                * np.cos(
                    (2 * k + 1)
                    * (np.pi / (2 * subbands))
                    * (np.arange(taps + 1) - (taps / 2))
                    - (-1) ** k * np.pi / 4
                )
            )

        # [subbands, 1, taps + 1] == [filter_width, in_channels, out_channels]
        analysis_filter = np.expand_dims(h_analysis, 1)
        analysis_filter = np.transpose(analysis_filter, (2, 1, 0))

        synthesis_filter = np.expand_dims(h_synthesis, 0)
        synthesis_filter = np.transpose(synthesis_filter, (2, 1, 0))

        # filter for downsampling & upsampling
        updown_filter = np.zeros((subbands, subbands, subbands), dtype=np.float32)
        for k in range(subbands):
            updown_filter[0, k, k] = 1.0

        self.subbands = subbands
        self.taps = taps
        self.analysis_filter = analysis_filter.astype(np.float32)
        self.synthesis_filter = synthesis_filter.astype(np.float32)
        self.updown_filter = updown_filter.astype(np.float32)

    @tf.function(
        experimental_relax_shapes=True,
        input_signature=[tf.TensorSpec(shape=[None, None, 1], dtype=tf.float32)],
    )
    def analysis(self, x):
        """Analysis with PQMF.
        Args:
            x (Tensor): Input tensor (B, T, 1).
        Returns:
            Tensor: Output tensor (B, T // subbands, subbands).
        """
        x = tf.pad(x, [[0, 0], [self.taps // 2, self.taps // 2], [0, 0]])
        x = tf.nn.conv1d(x, self.analysis_filter, stride=1, padding="VALID")
        x = tf.nn.conv1d(x, self.updown_filter, stride=self.subbands, padding="VALID")
        return x

    @tf.function(
        experimental_relax_shapes=True,
        input_signature=[tf.TensorSpec(shape=[None, None, None], dtype=tf.float32)],
    )
    def synthesis(self, x):
        """Synthesis with PQMF.
        Args:
            x (Tensor): Input tensor (B, T // subbands, subbands).
        Returns:
            Tensor: Output tensor (B, T, 1).
        """
        x = tf.nn.conv1d_transpose(
            x,
            self.updown_filter * self.subbands,
            strides=self.subbands,
            output_shape=(
                tf.shape(x)[0],
                tf.shape(x)[1] * self.subbands,
                self.subbands,
            ),
        )
        x = tf.pad(x, [[0, 0], [self.taps // 2, self.taps // 2], [0, 0]])
        return tf.nn.conv1d(x, self.synthesis_filter, stride=1, padding="VALID")
