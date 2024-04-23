import torch
from torch import nn

import modules.attentions as attentions
from modules.modules import AdainResBlk1d, ConditionalLayerNorm


class Encoder(nn.Module):
    def __init__(
        self,
        hidden_channels,
        filter_channels,
        n_heads,
        n_layers,
        dim_head=None,
        kernel_size=1,
        p_dropout=0.0,
        utt_emb_dim=512,
    ):
        super().__init__()
        self.hidden_channels = hidden_channels
        self.filter_channels = filter_channels
        self.n_heads = n_heads
        self.n_layers = n_layers
        self.kernel_size = kernel_size
        self.p_dropout = p_dropout

        self.drop = nn.Dropout(p_dropout)
        self.attn_layers = nn.ModuleList()

        self.norm_layers_1 = nn.ModuleList()
        self.norm_layers_2 = nn.ModuleList()

        self.ffn_layers_1 = nn.ModuleList()

        for i in range(self.n_layers):
            self.attn_layers.append(
                attentions.MultiHeadAttention(
                    hidden_channels,
                    hidden_channels,
                    n_heads,
                    dim_head=dim_head,
                    p_dropout=p_dropout,
                )
            )
            self.norm_layers_1.append(
                ConditionalLayerNorm(hidden_channels, utt_emb_dim)
            )

            self.ffn_layers_1.append(
                attentions.FFN(
                    hidden_channels,
                    hidden_channels,
                    filter_channels,
                    kernel_size=kernel_size,
                    p_dropout=p_dropout,
                    causal=True,
                )
            )
            self.norm_layers_2.append(
                ConditionalLayerNorm(hidden_channels, utt_emb_dim)
            )

    def forward(self, x, x_mask, g=None):
        # attn mask
        attn_mask = x_mask.unsqueeze(2) * x_mask.unsqueeze(-1)
        x = x * x_mask

        for i in range(self.n_layers):
            # self-attention
            y = self.attn_layers[i](x, x, attn_mask)
            y = self.drop(y)
            x = self.norm_layers_1[i](x + y, g)

            # feed-forward
            y = self.ffn_layers_1[i](x, x_mask)
            y = self.drop(y)
            x = self.norm_layers_2[i](x + y, g)

        x = x * x_mask
        return x


class ConditionalEmbedding(nn.Module):
    def __init__(self, num_embeddings, d_model, style_dim=512):
        super().__init__()
        self.embed = nn.Embedding(num_embeddings=num_embeddings, embedding_dim=d_model)
        self.adain = AdainResBlk1d(
            dim_in=d_model, dim_out=d_model, style_dim=style_dim, kernel_size=3
        )

    def forward(self, x, x_mask, utt_emb):
        emb = self.embed(x).transpose(1, 2)
        x = self.adain(emb, utt_emb)
        return x * x_mask


class AuxDecoder(nn.Module):
    def __init__(
        self,
        input_channels,
        hidden_channels,
        output_channels,
        kernel_size,
        n_layers,
        n_heads,
        dim_head=None,
        p_dropout=None,
        utt_emb_dim=0,
    ):
        super().__init__()

        self.aux_prenet = nn.Conv1d(
            input_channels, hidden_channels, kernel_size, padding=(kernel_size - 1) // 2
        )
        self.prenet = nn.Conv1d(
            hidden_channels, hidden_channels, kernel_size=3, padding=1
        )

        self.drop = nn.Dropout(p_dropout)

        self.aux_decoder = Encoder(
            hidden_channels=hidden_channels,
            filter_channels=hidden_channels * 4,
            n_heads=n_heads,
            n_layers=n_layers,
            kernel_size=kernel_size,
            dim_head=dim_head,
            utt_emb_dim=utt_emb_dim,
            p_dropout=0.1,
        )

        self.proj = nn.Conv1d(hidden_channels, output_channels, 1)

    def forward(self, x, x_mask, aux, utt_emb):
        # detach x
        x = torch.detach(x)

        # prenets
        x = x + self.aux_prenet(aux) * x_mask
        x = self.prenet(x) * x_mask

        # attention
        x = self.aux_decoder(x, x_mask, utt_emb)

        # out projection
        x = self.proj(x) * x_mask

        return x * x_mask


class VarianceDecoder(nn.Module):
    def __init__(
        self,
        input_channels,
        hidden_channels,
        output_channels,
        kernel_size=3,
        n_layers=2,
        n_blocks=2,
        p_dropout=0.1,
        utt_emb_dim=0,
    ):
        """
        Initialize variance encoder module.

        Args:
            input_channels (int): Number of input channels.
            hidden_channels (int): Number of hidden channels.
            output_channels (int): Number of output channels.
            kernel_size (int): Kernel size of convolution layers.
            n_layers (int): Number of layers.
            n_blocks (int): Number of blocks.
            p_dropout (float): Dropout probability.
            utt_emb_dim (int): Dimension of utterance embedding.
        """
        super().__init__()

        # prenet
        layers = []
        for _ in range(n_blocks):
            for _ in range(n_layers):
                layers.append(
                    nn.ModuleList(
                        [
                            torch.nn.Conv1d(
                                input_channels,
                                hidden_channels,
                                kernel_size,
                                padding=(kernel_size - 1) // 2,
                            ),
                            nn.LeakyReLU(0.2),
                            ConditionalLayerNorm(
                                hidden_channels, utt_emb_dim, epsilon=1e-6
                            ),
                            nn.Dropout(p_dropout),
                        ]
                    )
                )
                input_channels = hidden_channels

            layers.append(
                nn.GRU(
                    hidden_channels,
                    hidden_channels,
                    num_layers=1,
                    batch_first=True,
                    bidirectional=True,
                )
            )

        self.layers = nn.ModuleList(layers)

        self.proj = nn.Sequential(
            nn.Conv1d(hidden_channels, output_channels, 1),
            nn.InstanceNorm1d(output_channels, affine=True),
        )

    def forward(self, x, x_mask=None, utt_emb=None):
        # attention mask
        attn_mask = x_mask.unsqueeze(2) * x_mask.unsqueeze(-1)

        for layer in self.layers:
            if isinstance(layer, attentions.MultiHeadAttention):
                x = layer(x, x, attn_mask=attn_mask) + x
            else:
                conv, act, norm, drop = layer
                x = conv(x) * x_mask  # (B, C, Tmax)
                x = act(x)
                x = norm(x, utt_emb)
                x = drop(x)

        x = self.proj(x * x_mask)
        return x * x_mask
