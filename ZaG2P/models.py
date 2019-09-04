# -*- coding: utf-8 -*-

""" Created on 3:36 PM, 7/23/19
    @author: ngunhuconchocon
    @brief: Пролетарии всех стран, соединяйтесь!
"""
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import torch.optim as optim
from torch.nn.utils import clip_grad_norm
import torchtext.data as data


class Encoder(nn.Module):

    def __init__(self, vocab_size, d_embed, d_hidden):
        super(Encoder, self).__init__()
        self.embedding = nn.Embedding(vocab_size, d_embed)
        self.lstm = nn.LSTMCell(d_embed, d_hidden)
        self.d_hidden = d_hidden

    def forward(self, x_seq, cuda=False):
        o = []
        e_seq = self.embedding(x_seq)  # seq x batch x dim
        tt = torch.cuda if cuda else torch  # use cuda tensor or not
        # create initial hidden state and initial cell state
        h = Variable(tt.FloatTensor(e_seq.size(1), self.d_hidden).zero_())
        c = Variable(tt.FloatTensor(e_seq.size(1), self.d_hidden).zero_())

        for e in e_seq.chunk(e_seq.size(0), 0):
            e = e.squeeze(0)
            h, c = self.lstm(e, (h, c))
            o.append(h)
        return torch.stack(o, 0), h, c


# In[5]:
# class Encoder(nn.Module):
#
#     def __init__(self, vocab_size, d_embed, d_hidden):
#         super(Encoder, self).__init__()
#         self.embedding = nn.Embedding(vocab_size, d_embed)
#         self.lstm = nn.LSTMCell(d_embed, d_hidden)
#         self.d_hidden = d_hidden
#
#     def forward(self, x_seq, cuda=False):
#         o = []
#         e_seq = self.embedding(x_seq)  # seq x batch x dim
#         tt = torch.cuda if cuda else torch  # use cuda tensor or not
#         # create initial hidden state and initial cell state
#         h = Variable(tt.FloatTensor(e_seq.size(1), self.d_hidden).zero_())
#         c = Variable(tt.FloatTensor(e_seq.size(1), self.d_hidden).zero_())
#
#         for e in e_seq.chunk(e_seq.size(0), 0):
#             e = e.squeeze(0)
#             h, c = self.lstm(e, (h, c))
#             o.append(h)
#         return torch.stack(o, 0), h, c


class Attention(nn.Module):
    """Dot global attention from https://arxiv.org/abs/1508.04025"""

    def __init__(self, dim):
        super(Attention, self).__init__()
        self.linear = nn.Linear(dim * 2, dim, bias=False)

    def forward(self, x, context=None):
        if context is None:
            return x
        assert x.size(0) == context.size(0)  # x: batch x dim
        assert x.size(1) == context.size(2)  # context: batch x seq x dim
        attn = F.softmax(context.bmm(x.unsqueeze(2)).squeeze(2))
        weighted_context = attn.unsqueeze(1).bmm(context).squeeze(1)
        o = self.linear(torch.cat((x, weighted_context), 1))
        return torch.tanh(o)


class Decoder(nn.Module):

    def __init__(self, vocab_size, d_embed, d_hidden):
        super(Decoder, self).__init__()
        self.embedding = nn.Embedding(vocab_size, d_embed)
        self.lstm = nn.LSTMCell(d_embed, d_hidden)
        self.attn = Attention(d_hidden)
        self.linear = nn.Linear(d_hidden, vocab_size)

    def forward(self, x_seq, h, c, context=None):
        o = []
        e_seq = self.embedding(x_seq)
        for e in e_seq.chunk(e_seq.size(0), 0):
            e = e.squeeze(0)
            h, c = self.lstm(e, (h, c))
            o.append(self.attn(h, context))
        o = torch.stack(o, 0)
        o = self.linear(o.view(-1, h.size(1)))
        return F.log_softmax(o).view(x_seq.size(0), -1, o.size(1)), h, c


class G2P(nn.Module):

    def __init__(self, config):
        super(G2P, self).__init__()
        self.encoder = Encoder(config.g_size, config.d_embed,
                               config.d_hidden)
        self.decoder = Decoder(config.p_size, config.d_embed,
                               config.d_hidden)
        self.config = config

    def forward(self, g_seq, p_seq=None):
        o, h, c = self.encoder(g_seq, self.config.cuda)
        # context = o.t() if self.config.attention else None
        context = o.transpose(0, 1) if self.config.attention else None
        if p_seq is not None:  # not generate
            return self.decoder(p_seq, h, c, context)
        else:
            assert g_seq.size(1) == 1  # make sure batch_size = 1
            return self._generate(h, c, context)

    def _generate(self, h, c, context):
        beam = Beam(self.config.beam_size, cuda=self.config.cuda)
        # Make a beam_size batch.
        h = h.expand(beam.size, h.size(1))
        c = c.expand(beam.size, c.size(1))
        context = context.expand(beam.size, context.size(1), context.size(2))

        for i in range(self.config.max_len):  # max_len = 20
            x = beam.get_current_state()
            o, h, c = self.decoder(Variable(x.unsqueeze(0)), h, c, context)
            if beam.advance(o.data.squeeze(0)):
                break
            h.data.copy_(h.data.index_select(0, beam.get_current_origin()))
            c.data.copy_(c.data.index_select(0, beam.get_current_origin()))
        tt = torch.cuda if self.config.cuda else torch
        return Variable(tt.LongTensor(beam.get_hyp(0)))


# Based on https://github.com/MaximumEntropy/Seq2Seq-PyTorch/
class Beam(object):
    """Ordered beam of candidate outputs."""

    def __init__(self, size, pad=1, bos=2, eos=3, cuda=False):
        """Initialize params."""
        self.size = size
        self.done = False
        self.pad = pad
        self.bos = bos
        self.eos = eos
        self.tt = torch.cuda if cuda else torch

        # The score for each translation on the beam.
        self.scores = self.tt.FloatTensor(size).zero_()

        # The backpointers at each time-step.
        self.prevKs = []

        # The outputs at each time-step.
        self.nextYs = [self.tt.LongTensor(size).fill_(self.pad)]
        self.nextYs[0][0] = self.bos

    # Get the outputs for the current timestep.
    def get_current_state(self):
        """Get state of beam."""
        return self.nextYs[-1]

    # Get the backpointers for the current timestep.
    def get_current_origin(self):
        """Get the backpointer to the beam at this step."""
        return self.prevKs[-1]

    def advance(self, workd_lk):
        """Advance the beam."""
        num_words = workd_lk.size(1)

        # Sum the previous scores.
        if len(self.prevKs) > 0:
            beam_lk = workd_lk + self.scores.unsqueeze(1).expand_as(workd_lk)
        else:
            beam_lk = workd_lk[0]

        flat_beam_lk = beam_lk.view(-1)

        bestScores, bestScoresId = flat_beam_lk.topk(self.size, 0,
                                                     True, True)
        self.scores = bestScores

        # bestScoresId is flattened beam x word array, so calculate which
        # word and beam each score came from
        prev_k = bestScoresId / num_words
        self.prevKs.append(prev_k)
        self.nextYs.append(bestScoresId - prev_k * num_words)
        # End condition is when top-of-beam is EOS.
        if self.nextYs[-1][0] == self.eos:
            self.done = True
        return self.done

    def get_hyp(self, k):
        """Get hypotheses."""
        hyp = []
        # print(len(self.prevKs), len(self.nextYs), len(self.attn))
        for j in range(len(self.prevKs) - 1, -1, -1):
            hyp.append(self.nextYs[j + 1][k])
            k = self.prevKs[j][k]
        return hyp[::-1]
