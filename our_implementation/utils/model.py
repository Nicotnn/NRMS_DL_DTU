import numpy as np
import torch
import torch.nn as nn
from utils.helper import SEED

# Set random seed
torch.manual_seed(SEED)
np.random.seed(SEED)


class SelfAttention(nn.Module):
    def __init__(self, hparams, embedding_dim, verbose=False):
        super().__init__()
        self.head_num = hparams.head_num
        self.head_dim = hparams.head_dim
        self.output_dim = self.head_num * self.head_dim
        self.WQ = nn.Linear(embedding_dim, self.output_dim)
        self.WK = nn.Linear(embedding_dim, self.output_dim)
        self.WV = nn.Linear(embedding_dim, self.output_dim)
        self.dropout = nn.Dropout(hparams.dropout)
        self.verbose = verbose

    def forward(self, Q_seq, K_seq, V_seq):
        Q = self.WQ(Q_seq)
        K = self.WK(K_seq)
        V = self.WV(V_seq)

        N, L, _ = Q.size()
        Q = Q.view(N, L, self.head_num, self.head_dim).transpose(1, 2)
        K = K.view(N, L, self.head_num, self.head_dim).transpose(1, 2)
        V = V.view(N, L, self.head_num, self.head_dim).transpose(1, 2)

        if self.verbose:
            print(f"Q shape: {Q.shape}")
            print(f"K shape: {K.shape}")
            print(f"V shape: {V.shape}")

        scores = torch.matmul(Q, K.transpose(-2, -1)) / np.sqrt(self.head_dim)
        attn = torch.softmax(scores, dim=-1)
        attn = self.dropout(attn)

        output = torch.matmul(attn, V)
        output = output.transpose(
            1, 2).contiguous().view(N, L, self.output_dim)

        if self.verbose:
            print(f"Attention shape: {attn.shape}")
            print(f"Output shape: {output.shape}")

        return output


class AdditiveAttention(nn.Module):
    def __init__(self, hparams, verbose=False):
        super().__init__()
        self.W = nn.Linear(
            # Input size: hparams.head_num * hparams.head_dim, Output size: hparams.attention_hidden_dim
            hparams.head_num * hparams.head_dim, hparams.attention_hidden_dim
        )
        self.q = nn.Linear(hparams.attention_hidden_dim, 1, bias=False)
        self.dropout = nn.Dropout(hparams.dropout)
        self.verbose = verbose

    def forward(self, x):
        attn = torch.tanh(self.W(x))
        attn = self.q(attn).squeeze(-1)
        attn = torch.softmax(attn, dim=1).unsqueeze(-1)

        if self.verbose:
            print(f"Attention weights shape: {attn.shape}")
            print(f"Input shape: {x.shape}")

        output = torch.sum(x * attn, dim=1)
        output = self.dropout(output)

        if self.verbose:
            print(f"Output shape: {output.shape}")

        return output


class NRMSModel(nn.Module):
    def __init__(self, hparams, word_embeddings):
        super().__init__()
        embedding_dim = word_embeddings.shape[1]
        self.embedding = nn.Embedding.from_pretrained(
            torch.FloatTensor(word_embeddings), freeze=False
        )
        # Shape: (N_total, 1) -> (N_total, 768)
        self.time_embedding = nn.Linear(
            1, hparams.head_num * hparams.head_dim // 2)

        self.combine_proj = nn.Linear(hparams.head_num * hparams.head_dim +
                                      hparams.head_num * hparams.head_dim // 2, hparams.head_num * hparams.head_dim)

        self.dropout = nn.Dropout(hparams.dropout)

        # News Encoder
        self.news_self_att = SelfAttention(
            hparams, embedding_dim=embedding_dim, verbose=hparams.verbose)
        self.news_att = AdditiveAttention(hparams, verbose=hparams.verbose)

        # User Encoder
        self.user_self_att = SelfAttention(hparams, embedding_dim=(
            hparams.head_num*hparams.head_dim), verbose=hparams.verbose)
        self.user_att = AdditiveAttention(hparams, verbose=hparams.verbose)

    def encode_news(self, news_input, news_time=None):
        verbose = False
        # Step 1: Token-level Embeddings and Attention
        x = self.embedding(news_input)  # Shape: (N_total, L_tokens, D)
        x = self.dropout(x)
        x = self.news_self_att(x, x, x)  # Self-attention over tokens
        # Aggregate into single vector per article -> Shape: (N_total, D)
        x = self.news_att(x)
        if verbose:
            print(f"news_input shape: {news_input.shape}")
            print(f"news_time shape: {news_time.shape}")
            print(f"x shape: {x.shape}")
        # Step 2: Time Embedding at the Article Level
        if news_time is not None:
            # Shape: (batch_size * num_items)
            news_time = news_time.view(-1, 1)  # Shape: (N_total, 1)
            time_emb = self.time_embedding(news_time)  # Shape: (N_total, 4)
            time_emb = torch.tanh(time_emb)  # non-linearity
            x = torch.cat([x, time_emb], dim=-1)  # Concatenate
            x = self.combine_proj(x)  # Project back to (N_total, D)
        return x

    def encode_user(self, history_input, history_time=None):
        N, H, L = history_input.size()
        history_input = history_input.view(N * H, L)
        history_time = history_time.view(-1,
                                         1) if history_time is not None else None

        news_vectors = self.encode_news(history_input, history_time)
        news_vectors = news_vectors.view(N, H, -1)
        user_vector = self.user_self_att(
            news_vectors, news_vectors, news_vectors)
        user_vector = self.user_att(user_vector)
        return user_vector

    def forward(self, his_input, his_time, pred_input, pred_time):
        user_vector = self.encode_user(his_input, his_time)
        N, M, L = pred_input.size()
        pred_input = pred_input.view(N * M, L)
        news_vectors = self.encode_news(pred_input, pred_time)
        news_vectors = news_vectors.view(N, M, -1)
        scores = torch.bmm(news_vectors, user_vector.unsqueeze(2)).squeeze(-1)
        return scores
