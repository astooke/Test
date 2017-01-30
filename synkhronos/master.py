"""
Run theano functions in parallel on multiple GPUs (data parallelism).

This file has everything the master does.
"""

import pickle
import numpy as np
import multiprocessing as mp
import theano
import functools

from common import struct, Inputs, Shareds, SynkFunction
from common import use_gpu, init_gpu_comm
from common import (PKL_FILE, FUNCTION, GPU_COMM, BROADCAST, REDUCE, ALL_REDUCE,
                    ALL_GATHER, COLLECT_MODES, REDUCE_OPS, AVG_ALIASES, CPU_COMM,
                    SCATTER)


class Outputs(struct):

    def __init__(self, **kwargs):
        super(Outputs).__init__(self, **kwargs)
        self.vars = list()
        self.gpu_vars = list()
        self.to_cpu = list()
        self.dtypes = list()
        self.num = 0
        self.avg_funcs = list()
        self.avg_facs = list()

    def include(self, var):
        if var in self.vars:  # (already have this var, just retrieve it)
            otpt_ID = self.vars.index(var)
            gpu_var = self.gpu_vars[otpt_ID]
        else:
            otpt_ID = self.num
            self.vars.append(var)
            self.dtypes.append(var.type.dtype)
            GpuArrayVariable = get_gpuarray_class()  # (can't import in file header)
            to_cpu = False if isinstance(var, GpuArrayVariable) else True
            self.to_cpu.append(to_cpu)
            gpu_var = var.transfer(None)
            self.gpu_vars.append(gpu_var)
            avg_fac = theano.shared(np.array(1, dtype=var.type.dtype))
            avg_otpt = (avg_fac * gpu_var).transfer(None)
            self.avg_funcs.append(theano.function([gpu_var], avg_otpt))
            self.num += 1
        return otpt_ID, gpu_var

    def register(self, otpts):
        otpt_IDs = list()
        gpu_otpts = list()
        for var in otpts:
            otpt_ID, gpu_otpt = self.include(var)
            otpt_IDs.append(otpt_ID)
            gpu_otpts.append(gpu_otpt)
        return otpt_IDs, gpu_otpts

    def set_avg_facs(self, n_gpu):
        for avg_fac in self.avg_facs:
            avg_fac.set_value(1 / n_gpu)


# globals
g = struct(
    # State
    forked=False,
    distributed=False,
    closed=False,
    # Multiprocessing
    sync=None,
    processes=list(),
    # Theano
    inputs=Inputs(),
    shareds=Shareds(),
    outputs=Outputs(),
    theano_functions=list(),
    # GPU
    synk_functions=list(),
    n_gpu=None,
    gpu_comm=None,
    master_rank=None,
)


###############################################################################
#                                                                             #
#                           Building Functions.                               #
#                                                                             #
###############################################################################


def alloc_write_shmem(input_arg, input_ID):
    shape = list(input_arg.shape)
    shape[0] = int(np.ceil(shape[0] * 1.05))  # ( a little extra)
    tag_ID = np.max(g.sync.input_tag_IDs) + 1
    shmem = g.inputs.alloc_shmem(input_ID, shape, tag_ID)
    shmem[:input_arg.shape[0]] = input_arg  # (copy arg data into shared buffer)
    g.sync.input_tag_IDs[input_ID] = tag_ID
    return shmem


class Function(SynkFunction):

    def __init__(self, input_names, output_IDs,
                 *args, **kwargs):
        super(Function).__init__(*args, **kwargs)
        self._call = self._pre_distributed_call
        self._input_names = input_names
        self._output_IDs = output_IDs
        self._previous_batch_size = None
        self._my_idx = None
        self._n_inputs = len(self._input_IDs)

    def __call__(self, *args, **kwargs):
        self._call(*args, **kwargs)  # What this refers to is set dynamically

    @property
    def outputs_to_cpu(self):
        return self._outputs_to_cpu

    def _set_normal_call(self):
        self._call = self._synk_call

    def _close(self):
        self._call = self._closed_call

    def _order_inputs(self, *args, **kwargs):
        ordered_inputs = list(args)
        if not kwargs:
            output_subset = None
        else:
            output_subset = kwargs.pop("output_subset", None)
            if output_subset is not None:
                raise NotImplementedError
            ordered_inputs += [None] * len(kwargs)
            try:
                for name, val in kwargs.iteritems():
                    ordered_inputs[self._input_names.index(name)] = val
            except ValueError as e:
                raise e("Input passed as keyword arg not found in input names \
                    of function.")
        if len(ordered_inputs) != self._n_inputs:
            raise TypeError("Incorrect number of inputs to synkhronos function.")
        return ordered_inputs, output_subset

    def _update_batch_size(self, ordered_inputs):
        for idx, scatter in enumerate(self._inputs_scatter):
            if scatter:
                batch_size = ordered_inputs[idx].shape[0]
                break
        else:
            return  # (all inputs broadcast, no batch_size for data parallel)
        for scatter, inpt in zip(self._inputs_scatter, ordered_inputs):
            if scatter and inpt.shape[0] != batch_size:
                raise ValueError("Scatter Inputs of different batch sizes \
                    (using 0-th index).")
        if batch_size != self._previous_batch_size:
            assign_idx = int(np.ceil(np.linspace(0, batch_size, g.n_gpu + 1)))
            g.sync.assign_idx[self._ID, :] = assign_idx
            self._my_idx = (assign_idx[g.master_rank],
                            assign_idx[g.master_rank + 1])
            self._previous_batch_size = batch_size

    def _update_shmem(self, inpt, inpt_ID):
        shmem = g.inputs.shmems[inpt_ID]
        if shmem is None:
            shmem = alloc_write_shmem(inpt, inpt_ID)
        else:
            # check if they are already the same memory (based on first element)
            inpt_addr, _ = inpt.__array_interface__["data"]
            shmem_addr, _ = shmem.__array_interface__["data"]
            if inpt_addr == shmem_addr:
                if inpt.__array_interface__["strides"] is not None:
                    raise ValueError("Cannot accept strided view of existing \
                        shared memory as input.")
            else:
                if inpt.shape[1:] != shmem.shape[1:] or \
                        inpt.shape[0] > shmem.shape[0]:
                    # new shape or bigger batch
                    shmem = alloc_write_shmem(inpt, inpt_ID)
                else:
                    shmem[:inpt.shape[0]] = inpt  # already enough shared memory
        return shmem

    def _share_inputs(self, *args, **kwargs):
        """
        Can separately be used to allocate, write, and return input shared
        memory arrays without executing the function.
        """
        if not args and not kwargs:
            return None, None, None
        ordered_inputs, output_subset = self._order_inputs(*args, **kwargs)
        self._update_batch_size(ordered_inputs)
        my_inputs = list()
        input_shmems = list()
        for inpt, inpt_ID, scatter in zip(ordered_inputs, self._input_IDs, self._inputs_scatter):
            shmem = self._update_shmem(inpt, inpt_ID)
            if scatter:
                my_inputs.append(shmem[self._my_idx[0]:self._my_idx[1]])
            else:
                my_inputs.append(shmem[:inpt[0]])
            input_shmems.append(shmem)
        return tuple(my_inputs), output_subset, input_shmems

    def _set_worker_signal(self):
        self._sync.exec_type.value = FUNCTION
        self._sync.func_ID.value = self._ID
        self._sync.barriers.exec_in.wait()

    def _collect_results(self, my_results):
        results = list()
        for (idx, r), mode, op in zip(enumerate(my_results),
                                      self.collect_modes, self.reduce_ops):
            if mode == "reduce":
                if op in AVG_ALIASES:
                    g.gpu_comm.reduce(r, op="sum", dest=r)
                    # Then do the average (maybe do in separate loop)
                    r = g.outputs.avg_funcs[self.output_IDs[idx]](r)
                else:
                    g.gpu_comm.reduce(r, op=op, dest=r)  # (in-place)
                results.append(r)
            elif mode == "gather":
                res = g.gpu_comm.all_gather(r)  # TODO: figure out exactly what this returns
                results.append(res)
            elif mode is None:
                results.append(r)
            else:
                raise RuntimeError("Unrecognized collect mode in master function.")

        return results

    def _closed_call(self, *args):
        raise RuntimeError("Synkhronos already closed, can only call Theano \
            function.")

    def _pre_distributed_call(self, *args):
        raise RuntimeError("Synkhronos functions have not been distributed to \
            workers, can only call Theano function.")

    def _synk_call(self, *args, **kwargs):
        """
        This needs to:
        1. Share input data.
        2. Signal to workers to start and what to do.
        3. Call the local theano function on data.
        4. Collect result from workers and return it.

        NOTE: Barriers happen INSIDE master function call.
        """
        return_shmems = kwargs.pop("return_shmems", False)
        my_inputs, output_subset, input_shmems = self._share_inputs(*args,
                                                                    **kwargs)
        self._set_worker_signal()
        my_results = self._call_theano_function(my_inputs, output_subset)  # always a list
        results = self._collect_results(my_results)  # returns a list
        for idx, otpt_ID in enumerate(self.output_IDs):
            if g.outputs.to_cpu[otpt_ID]:
                results[idx] = np.array(results[idx])
        self._sync.barriers.exec_out.wait()  # NOTE: Not sure if keeping this--yes
        if return_shmems:
            results += input_shmems  # append list of results with tuple of shmems
        if len(results) == 1:
            results = results[0]
        return results

    def get_input_shmems(self, *args, **kwargs):
        if self._call is not self._synk_call:
            raise RuntimeError("Cannot call this method on inactive synkhronos \
                function.")
        input_shmems = list()
        if not args and not kwargs:
            # gather existing
            for inpt_ID in self._input_IDs:
                input_shmems.append(g.inputs.shmems[inpt_ID])
        else:
            # make new ones according to inputs
            _, _, input_shmems = self._share_inputs(*args, **kwargs)
        return input_shmems


def function(inputs, outputs=None, updates=None, name=None,
             collect_modes="reduce", reduce_ops="avg",
             broadcast_inputs=None, scatter_inputs=None,
             **kwargs):
    """
    Call this in the master process when normally creating a theano function.

    What does it need to do:
    1. Create & compile theano function.
       a. Register this function to be pickled later (or just do it now?).
    2. Register the inputs to be made into mp shared variables. (well, no, they
       already will be shared variables, but somehow associate them?)
       a. maybe have the user also input the shared variables here.
    """
    if not g.forked:
        raise RuntimeError("Must fork before making functions for GPU.")
    if g.distributed:
        raise RuntimeError("Cannot make new functions after distributing.")

    inputs_scatter = check_inputs_scatter(inputs, broadcast_inputs, scatter_inputs)
    collect_modes, reduce_ops = check_collect(outputs, collect_modes, reduce_ops)
    output_IDs, gpu_outputs = g.outputs.register(outputs)

    # TODO: Probably still need to do something about updates and givens.
    theano_function = theano.function(inputs=inputs,
                                      outputs=gpu_outputs,
                                      updates=updates,
                                      name=name,
                                      **kwargs,
                                      )
    g.theano_functions.append(theano_function)
    input_IDs, input_names = g.inputs.register_func(theano_function)
    shared_IDs = g.shareds.register_func(theano_function)
    synk_function = Function(name=name,
                             ID=len(g.synk_functions),  # Fcn can ID itself
                             theano_function=theano_function,
                             input_IDs=input_IDs,
                             input_names=input_names,
                             inputs_scatter=inputs_scatter,
                             shared_IDs=shared_IDs,
                             output_IDs=output_IDs,
                             collect_modes=collect_modes,
                             reduce_ops=reduce_ops,
                             )
    g.synk_functions.append(synk_function)
    return synk_function


###############################################################################
#                                                                             #
#                      GPU Collectives.                                       #
#                                                                             #
###############################################################################


def get_shared_IDs(synk_functions=None, shared_names=None):
    if synk_functions is None and shared_names is None:
        return tuple(range(g.shareds.num))  # default is all shareds
    else:
        # Type and existence checking.
        if not isinstance(synk_functions, (list, tuple)):
            synk_functions = (synk_functions,)
        for synk_fcn in synk_functions:
            if not isinstance(synk_fcn, Function):
                raise TypeError("Expected Synkhronos function(s).")
        if not isinstance(shared_names, (list, tuple)):
            shared_names = (shared_names,)
        for name in shared_names:
            if name not in g.shareds.names:
                raise ValueError("Unrecognized name for shared variable: ",
                    name)

        shared_IDs = list()
        for synk_fcn in synk_functions:
            shared_IDs += synk_fcn.shared_IDs
        for name in shared_names:
            shared_IDs.append(g.shareds.names.index(name))
    return tuple(sorted(set(shared_IDs)))


def gpu_comm_function(gpu_comm_func, comm_ID, has_op=False):
    def build_comm_procedure(f):
        @functools.wraps(f)  # (preserves signature and docstring of wrapped)
        def gpu_comm_procedure(functions=None, shared_names=None, op=None,
                               **kwargs):
            if g.closed:
                raise RuntimeError("synk already closed--cannot call comm \
                    function.")
            g.sync.exec_type = GPU_COMM
            g.sync.comm_ID = comm_ID
            shared_IDs = get_shared_IDs(functions, shared_names)
            n_shared = len(shared_IDs)
            g.sync.shared_IDs[:n_shared] = shared_IDs
            g.sync.n_shared.value = n_shared
            if has_op:
                op_ID = check_op(op)
                kwargs["op"] = op
                g.sync.comm_op.value = op_ID
            g.sync.barriers.exec_in.wait()
            results = list()
            for idx in shared_IDs:
                r = gpu_comm_func(idx, **kwargs)
                results.append(r)
            g.sync.barriers.exec_out.wait()
            return results  # (mostly just in case of non-in-place operation)
        return gpu_comm_procedure
    return build_comm_procedure


def reduce_func(idx, op, in_place=True, dest=None):
    avg = op in AVG_ALIASES
    op = "sum" if avg else op
    src = g.shareds.gpuarrays[idx]
    if in_place:
        g.gpu_comm.reduce(src, op=op, dest=src)
        if avg:
            g.shareds.avg_functions[idx]()
        return g.shareds.vars[idx]
    else:
        if avg:
            raise NotImplementedError  # Because I don't know what comes out.
        return g.gpu_comm.reduce(src, op=op, dest=dest)  # makes a new gpuarray


def all_reduce_func(idx, op):
    # workers can't get new arrays; everyone (including master) overwrites src
    avg = op in AVG_ALIASES
    op = "sum" if avg else op
    src = g.shareds.gpuarrays[idx]
    g.gpu_comm.all_reduce(src, op=op, dest=src)
    if avg:
        g.shareds.avg_functions[idx]()
    return g.shareds.vars[idx]


def broadcast_func(idx):
    src = g.shareds.gpuarrays[idx]
    g.gpu_comm.broadcast(src)
    return g.shareds.vars[idx]


def all_gather_func(idx, dest=None, nd_up=1):
    src = g.shareds.gpuarrays[idx]
    # Still not sure what this returns.
    return g.gpu_comm.all_gather(src, dest=dest, nd_up=nd_up)


@gpu_comm_function(broadcast_func, BROADCAST)
def broadcast(functions=None, shared_names=None):
    """broadcast docstring"""
    pass


@gpu_comm_function(all_reduce_func, ALL_REDUCE, has_op=True)
def all_reduce(functions=None, shared_names=None, op="avg"):
    """all_reduce docstring"""
    pass


@gpu_comm_function(reduce_func, REDUCE, has_op=True)
def reduce(functions=None, shared_names=None, op="avg", in_place=True,
           dest=None):
    """reduce docstring"""
    pass


@gpu_comm_function(all_gather_func, ALL_GATHER)
def all_gather(functions=None, shared_names=None, dest=None, nd_up=1):
    """all_gather docstring"""
    pass


###############################################################################
#                                                                             #
#                         CPU-based Communications                            #
#                                                                             #
###############################################################################


def check_shared_var(shared_var):
    if shared_var not in g.shareds.vars and shared_var not in g.shareds.names:
        raise ValueError("Unrecognized theano shared variable or name: ",
            shared_var)
    if shared_var in g.shareds.vars:
        shared_ID = g.shareds.vars.index(shared_var)
    else:
        shared_ID = g.shareds.names.index(shared_var)
        shared_var = g.shareds.vars[shared_ID]
    return shared_var, shared_ID


def check_scatter_sources(sources, shared_ID):
    if not isinstance(sources, (tuple, list)):
        raise TypeError("Param sources must be a list or tuple of arguments to shared.set_value().")
    if len(sources) != g.n_gpu:
        raise ValueError("Source list must have as many elements as there are GPUs.")
    for src in sources:
        if not isinstance(src, np.ndarray):
            raise TypeError("For now...Must provide a numpy ndarray for each source.")
    shared_shape = g.shareds.gpuarrays[shared_ID].shape
    shared_dtype = g.shareds.vars[shared_ID].type.dtype
    for src in sources:
        if src.dtype != shared_dtype:
            raise TypeError("Must provide the same data type as the shared var: ",
                shared_dtype)
        if src.shape != shared_shape:
            raise ValueError("Source is not same shape as shared variable: ",
                shared_shape)


def scatter(shared_var, sources):
    shared_var, shared_ID = check_shared_var(shared_var)
    check_scatter_sources(sources, shared_ID)
    if g.shareds.shmems[shared_ID] is None:
        g.shareds.build_shmems(shared_ID, g.n_gpu, g.master_rank)
    for rank, src in enumerate(sources):
        if rank == g.master_rank:
            shared_var.set_value(src)
        else:
            g.shareds.shmems[shared_ID][rank][:] = src
    g.sync.exec_type.value = CPU_COMM
    g.sync.comm_id.value = SCATTER
    g.sync.shared_IDs[0] = shared_ID  # (can only to one per call)
    g.sync.barriers.exec_in.wait()
    g.sync.barriers.exec_out.wait()


###############################################################################
#                                                                             #
#                       Initializing and Exiting.                             #
#                                                                             #
###############################################################################


def close():
    """
    It needs to:
    1. Signal all the workers to quit.
    2. join() the subprocesses.
    3. Turn off the synk functions.
    """
    if not g.forked:
        return
    elif not g.sync.distributed.value:  # (will be closing due to error)
        g.sync.barriers.distribute.wait()  # (Workers will know to exit)
        for p in g.processes:
            p.join()
    elif not g.closed:
        g.sync.quit.value = True
        g.sync.barriers.exec_in.wait()
        for p in g.processes:
            p.join()
        for synk_fcn in g.synk_functions:
            synk_fcn._close()  # Turn off all the functions.
        g.closed = True


def fork(n_gpu=None, master_rank=0):
    """
    Call this in the master process at the very beginning (possibly before
    importing theano, in order to use different GPUs)

    It needs to do:
    1. Build shared variables according to inputs (sizes & types).
       a. perhaps inputs is a list, which each entry a dict with: name, shape,
          type
       b. I can make this a separate object which the user simply populates, all
          format / layout is controlled.
       c. And maybe having these inputs stored in standardized way, easy to
          refer to them and check that they exist when making a function.
    2. Build synchronization (barriers, semaphores, etc.)
    3. Build whatever comms necessary for later creating functions & shared
       variables in subprocesses.
    4. Set os.environ THEANO_FLAGS for cuda devices, and fork subprocesses
       a. Don't necessarily need to start them now?
    """
    if g.forked:
        raise RuntimeError("Only fork once.")
    from worker import worker_exec

    n_gpu, master_rank = n_gpu(n_gpu, master_rank)
    sync = build_sync(n_gpu)

    for rank in [r for r in range(n_gpu) if r != master_rank]:
        args = (rank, n_gpu, master_rank, sync)
        g.processes.append(mp.Process(target=worker_exec, args=args))
    for p in g.processes:
        p.start()

    import atexit
    atexit.register(close)

    g.forked = True
    g.n_gpu = n_gpu
    g.master_rank = master_rank
    g.sync = sync

    use_gpu(master_rank)
    return n_gpu


def distribute_functions():
    """
    Call this in the master after having built all functions.

    It needs to do:
    1. Pickle all the functions.
    2. Make a map of which theano shared variables are the same, between
       functions.
    3. Pass the filename and the theano shared variable map to subprocesses.
    4. Tell subprocesses to get the functions and how to match theano shareds.
       a. (subprocesses will start with same shared values as in master)
    5. Wait until subprocesses are done.
    """
    if not g.forked:
        raise RuntimeError("Need to fork before distributing functions.")
    if g.distributed:
        raise RuntimeError("Can distribute only once.")

    avg_functions = g.shareds.build_avg_functions()
    pkl_functions = g.theano_functions + avg_functions  # (combine lists)

    # NOTE: pickle all functions together in one list to preserve
    # correspondences among shared variables in different functions.
    with open(PKL_FILE, "wb") as f:
        pickle.dump(pkl_functions, f, pickle.HIGHEST_PROTOCOL)

    gpu_comm, comm_id = init_gpu_comm(g.n_gpu, g.master_rank)
    g.sync.dict["comm_id"] = comm_id  # workers need it to join gpu comm clique
    g.sync.n_user_fcns.value = len(g.theano_functions)
    g.sync.dict["collect_modes"] = [fn._collect_modes for fn in g.synk_functions]
    g.sync.dict["reduce_ops"] = get_worker_reduce_ops(g.synk_functions)
    g.sync.dict["inputs_scatter"] = [fn._inputs_scatter for fn in g.synk_functions]
    g.sync.distributed.value = True  # let the workers know this part succeeded
    g.sync.barriers.distribute.wait()  # signal workers to receive & join comm
    g.inputs.build_sync(len(g.functions), g.n_gpu)
    g.shareds.build_sync()
    g.shareds.set_avg_facs(g.n_gpu)
    g.outputs.set_avg_facs(g.n_gpu)
    for synk_fcn in g.synk_functions:
        synk_fcn._set_normal_call()
    g.gpu_comm = gpu_comm
    g.distributed = True


###############################################################################
#                                                                             #
#                       Utilities (no accesses to global g)                   #
#                                                                             #
###############################################################################


def n_gpu_getter(mp_n_gpu):
    """
    Call in a subprocess because it prevents future subprocesses from using GPU.
    """
    from pygpu import gpuarray
    mp_n_gpu.value = gpuarray.count_devices("cuda", 0)


def n_gpu(n_gpu, master_rank):
    if n_gpu is not None:
        n_gpu = int(n_gpu)
    master_rank = int(master_rank)

    if n_gpu is None:
        #  Detect the number of devices present and use all.
        mp_n_gpu = mp.RawValue('i', 0)
        p = mp.Process(target=n_gpu_getter, args=(mp_n_gpu))
        p.start()
        p.join()
        n_gpu = mp_n_gpu.value
        if n_gpu == 0:
            raise RuntimeError("No cuda GPU detected by pygpu.")
        elif n_gpu == 1:
            raise RuntimeWarning("Only one GPU detected; undetermined behavior \
                (but I could make it revert to regular Theano?)")
        else:
            print("Detected and attempting to use {} GPUs.".format(n_gpu))

    if master_rank not in list(range(n_gpu)):
        raise ValueError("Invalid value for master rank: ", master_rank)

    return n_gpu, master_rank


def build_sync(n_gpu):
    from ctypes import c_bool

    mgr = mp.Manager()
    dictionary = mgr.dict()
    barriers = struct(
        distribute=mp.Barrier(n_gpu),
        exec_in=mp.Barrier(n_gpu),
        exec_out=mp.Barrier(n_gpu),
    )
    sync = struct(
        dict=dictionary,  # use for setup e.g. Clique comm_id; serializes.
        quit=mp.RawValue(c_bool, False),
        n_user_fcns=mp.RawValue('i', 0),
        distributed=mp.RawValue(c_bool, False),
        exec_type=mp.RawValue('i', 0),
        func_ID=mp.RawValue('i', 0),
        comm_ID=mp.RawValue('i', 0),
        n_shared=mp.RawValue('i', 0),
        barriers=barriers,
    )
    return sync


def get_gpuarray_class():
    from theano.gpuarray.type import GpuArrayVariable
    return GpuArrayVariable


def check_collect(outputs, collect_modes, reduce_ops):
    if outputs is None:
        if collect_modes is not None or reduce_ops is not None:
            raise RuntimeWarning("No function outputs, ignoring collect_modes \
                and reduce_ops parameters.")
        return None, None
    n_outputs = len(outputs)
    if not isinstance(collect_modes, (list, tuple)):
        collect_modes = [collect_modes] * n_outputs
    if len(collect_modes) != n_outputs:
        raise ValueError("Number of collect modes does not match number of \
            outputs (or enter a single string to be used for all outputs).")
    for mode in collect_modes:
        if mode not in COLLECT_MODES:
            raise ValueError("Unrecognized collect_mode: ", mode)
    if not isinstance(reduce_ops, (list, tuple)):
        tmp_ops = list()
        for mode in collect_modes:
            if mode == "reduce":
                tmp_ops.append(reduce_ops)
            else:
                tmp_ops.append(None)
        reduce_ops = tmp_ops
    if "reduce" not in collect_modes and \
            any([op is not None for op in reduce_ops]):
        raise RuntimeWarning("Reduce op(s) provided but ignored--no reduced \
            outputs.")
    if len(reduce_ops) != n_outputs:
        raise ValueError("Number of reduce ops does not match number of \
            outputs (use None for non-reduce outputs, or a single string for \
            all reduced outputs).")
    else:
        for idx, op in enumerate(reduce_ops):
            if collect_modes[idx] == "reduce":
                if op not in REDUCE_OPS:
                    raise ValueError("Unrecognized reduce op: ", op)
            else:
                if op is not None:
                    raise RuntimeWarning("Reduce op provided but ignored for \
                        non-reduce collect mode.")
    return collect_modes, reduce_ops


def check_op(op):
    if op not in REDUCE_OPS:
        raise ValueError("Unrecognized reduction operator: ", op,
            ", must be one of: ", [k for k in REDUCE_OPS.keys()])
    return REDUCE_OPS[op]


def get_worker_reduce_ops(synk_functions):
    reduce_ops_all = [fn.reduce_ops for fn in synk_functions]
    for ops in reduce_ops_all:
        for idx, op in enumerate(ops):
            if op in AVG_ALIASES:
                ops[idx] = "sum"
    return reduce_ops_all


def check_inputs_scatter(inputs, broadcast_inputs, scatter_inputs):
    if broadcast_inputs is not None and scatter_inputs is not None:
        raise ValueError("May specify either broadcast_inputs or scatter_inputs but not both.")
    if broadcast_inputs is None and scatter_inputs is None:
        inputs_scatter = [True] * len(inputs)  # (default is to scatter all)
    elif broadcast_inputs is not None:
        if not isinstance(broadcast_inputs, (tuple, list)):
            raise TypeError("Optional param broadcast_inputs must be list or tuple.")
        inputs_scatter = [True] * len(inputs)
        for bc_inpt in broadcast_inputs:
            if bc_inpt not in inputs:
                raise ValueError("Elements of param broadcast_inputs must also be inputs.")
            inputs_scatter[inputs.index(bc_inpt)] = False
    else:  # (scatter_inputs is not None)
        if not isinstance(scatter_inputs, (list, tuple)):
            raise TypeError("Optional param scatter_inputs must be list or tuple.")
        inputs_scatter = [False] * len(inputs)
        for sc_inpt in scatter_inputs:
            if sc_inpt not in inputs:
                raise ValueError("Elements of param scatter_inputs must also be inputs.")
            inputs_scatter[inputs.index(sc_inpt)] = True
    return inputs_scatter
