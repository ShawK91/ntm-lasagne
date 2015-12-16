import theano
import theano.tensor as T
import numpy as np

from lasagne.layers import Layer, InputLayer
import lasagne.init
import lasagne.layers.helper as helper
from heads import ReadHead, WriteHead


class NTMLayer(Layer):
    """
    docstring for NTMLayer
    """
    def __init__(self, incoming,
                 memory,
                 controller,
                 heads,
                 grad_clipping=None,
                 **kwargs):
        super(NTMLayer, self).__init__(incoming, **kwargs)

        # Populate the HeadLayers with memory & previous layers
        self.memory = memory
        self.controller = controller
        # TODO: Sort the heads to have WriteHeads > ReadHeads
        self.heads = heads
        self.grad_clipping = grad_clipping

    def get_output_shape_for(self, input_shapes):
        return (input_shapes[0], input_shapes[1], self.controller.num_units)

    def get_params(self, **tags):
        params = super(NTMLayer, self).get_params(**tags)
        params += self.controller.get_params(**tags)
        params += self.memory.get_params(**tags)
        for head in self.heads:
            params += head.get_params(**tags)

        return params

    def get_output_for(self, input, get_details=False, **kwargs):

        input = input.dimshuffle(1, 0, 2)

        def step(x_t, M_tm1, h_tm1, state_tm1, *params):
            # In the list params there are, in that order
            #   - w_tm1 for all the writing heads
            #   - w_tm1 for all the reading heads
            #   - Additional requirements for the controller (e.g. c_tm1 for LSTM)
            #   - W_hid_to_key, b_hid_to_key, ... for all the writing heads (14)
            #   - W_hid_to_key, b_hid_to_key, ... for all the reading heads (10)
            #   - Controller parameters (e.g. W & b for Dense)
            #   - Additional initial req. for the controller (e.g. c_0 for LSTM)
            num_write_heads = len(filter(lambda head: isinstance(head, WriteHead), self.heads))
            num_read_heads = len(filter(lambda head: isinstance(head, ReadHead), self.heads))
            num_heads = num_write_heads + num_read_heads
            outputs_t = []

            # Update the memory (using w_tm1 of the writing heads & M_tm1)
            M_t = M_tm1
            # Erase
            for i in range(num_write_heads):
                erase = self.heads[i].erase.get_output_for(h_tm1, **kwargs)
                erasing, _ = theano.map(T.outer, \
                    sequences=[params[i], erase])
                M_t *= 1. - erasing
            # Add
            for i in range(num_write_heads):
                if self.heads[i].sign_add is not None:
                    sign = self.heads[i].sign_add.get_output_for(h_tm1, **kwargs)
                else:
                    sign = 1.
                add = self.heads[i].add.get_output_for(h_tm1, **kwargs)
                adding, _ = theano.map(T.outer, \
                    sequences=[params[i], sign * add])
                M_t += adding
            outputs_t.append(M_t)

            # Get the read vector (using w_tm1 of the reading heads & M_t)
            read_vectors = []
            for i in range(num_write_heads, num_heads):
                reading, _ = theano.map(T.dot, sequences=[params[i], M_t])
                read_vectors.append(reading)
            r_t = T.concatenate(read_vectors)

            # Apply the controller (using x_t, r_t & requirements for the controller)
            h_t, state_t = self.controller.step(x_t, r_t, state_tm1)
            outputs_t.append(h_t)
            outputs_t.append(state_t)

            # Update the weights (using h_t, M_t & w_tm1)
            for i in range(num_heads):
                weights = self.heads[i].get_output_for(h_t, params[i], M_t, **kwargs)
                outputs_t.append(weights)

            # Gradient clipping
            # if self.grad_clipping is not None:
            #     outputs_t = [theano.gradient.grad_clip(param, -self.grad_clipping, \
            #         self.grad_clipping) for param in outputs_t]

            return outputs_t

        # TODO: hid_init and state_init for the Controller
        # QKFIX: Duplicate controller.hid_init for FeedForward Controller
        ones_vector = T.ones((self.input_shape[0], 1))
        memory_init = T.tile(self.memory.memory_init, (self.input_shape[0], 1, 1))
        hid_init = T.dot(ones_vector, self.controller.hid_init)
        outs_info = [T.unbroadcast(memory_init, 1), hid_init, hid_init]
        outs_info += [T.dot(ones_vector, head.weights_init) for head in self.heads]

        # QKFIX: Remove the strict mode
        hids, _ = theano.scan(
            fn=step,
            sequences=input,
            outputs_info=outs_info,
            # non_sequences=non_seqs,
            strict=False)

        # dimshuffle back to (n_batch, n_time_steps, n_features))
        if get_details:
            hid_out = [hids[0].dimshuffle(1, 0, 2, 3)]
            hid_out += [hid.dimshuffle(1, 0, 2) for hid in hids[1:]]
        else:
            hid_out = hids[1].dimshuffle(1, 0, 2)

        return hid_out


if __name__ == '__main__':
    import lasagne.layers
    inp = lasagne.layers.InputLayer((None, None, 10))
    ntm = NTM(inp, memory_shape=(128, 20), heads=[])