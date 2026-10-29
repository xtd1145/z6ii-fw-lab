# SPDX-License-Identifier: GPL-2.0-or-later
"""Original Z62 C1.70 service calls in an owned QEMU guest task.

The lab task is instrumentation, not camera firmware or USB emulation. It is
allocated and started through the original heap/kernel APIs. No service flag,
authorization byte or command return value is supplied by this harness.
"""
import json
import re
import struct
import time

PTP_WAIT = 0x1048c38e
MALLOC = 0x1034c1f0
CREATE_TASK = 0x103debc6
START_TASK = 0x103dec6a
DELAY = 0x103dec95
DATA_ABORT = 0x10100010


def stopped_thread(reply):
    found = re.search(r'thread:([0-9a-f]+);', reply or '')
    if not found:
        raise RuntimeError('Missing stopped CPU: ' + repr(reply))
    return int(found.group(1), 16)


def continue_until(remote, deadline):
    while time.monotonic() < deadline:
        remote.sock.settimeout(2)
        try:
            result = remote.command('vCont;c')
            remote.sock.settimeout(15)
            return result
        except TimeoutError:
            remote.sock.settimeout(15)
            remote.interrupt()
    raise TimeoutError('Native guest call did not finish')


def bootstrap_call(remote, thread, function, arguments, timeout=30):
    """Inferior call only at the original PTPSIC wait-loop safe point."""
    remote.select_thread(thread)
    original = remote.regs()
    if remote.get(original, 'pc') != PTP_WAIT:
        raise RuntimeError('Bootstrap requires the original PTPSIC loop boundary')
    stack = remote.get(original, 'sp')
    # Save the unused caller-stack region touched by this instrumentation.
    saved_stack = remote.read(stack - 0x800, 0x800)
    values = {f'r{i}': arguments[i] if i < len(arguments) else 0 for i in range(4)}
    remote.set(**values, pc=function & ~1, lr=PTP_WAIT | 1,
               cpsr=remote.get(original, 'cpsr') | 0x20)
    reply = continue_until(remote, time.monotonic() + timeout)
    returned_thread = stopped_thread(reply)
    remote.select_thread(returned_thread)
    returned = remote.regs()
    if remote.get(returned, 'pc') != PTP_WAIT or remote.get(returned, 'sp') != stack:
        raise RuntimeError('Native bootstrap did not return to its task: ' + repr({
            'reply': reply, 'pc': hex(remote.get(returned, 'pc')),
            'sp': hex(remote.get(returned, 'sp')), 'lr': hex(remote.get(returned, 'lr'))}))
    result = remote.get(returned, 'r0')
    remote.write(stack - 0x800, saved_stack)
    if remote.command('G' + original.hex()) != 'OK':
        raise RuntimeError('Cannot restore the instrumented task context')
    return result, returned_thread


def task_code(address, mailbox):
    """Small Thumb polling task; original dly_tsk yields between requests."""
    words = []
    labels = {}
    fixes = []
    def emit(value): words.append(value)
    def label(name): labels[name] = len(words) * 2
    def literal(register, value):
        fixes.append(('literal', len(words) * 2, register, value))
        emit(0)
    def branch(name, condition=None):
        fixes.append(('branch', len(words) * 2, condition, name))
        emit(0)
    emit(0xb5f0)  # push {r4-r7,lr}
    emit(0xb081)  # preserve AAPCS 8-byte stack alignment
    literal(4, mailbox)
    literal(6, DELAY)
    emit(0x2001)
    emit(0x61e0)  # task-entry witness at mailbox+1ch
    label('poll')
    emit(0x6820)  # ldr r0,[r4]
    emit(0x2801)
    branch('work', 0)
    emit(0x2001)
    emit(0x47b0)  # blx r6 (original task delay)
    branch('poll')
    label('work')
    emit(0x2002)
    emit(0x6020)
    emit(0x6865)  # function at +4
    emit(0x68a0)  # r0 = argument0
    emit(0x68e1)
    emit(0x6922)
    emit(0x6963)
    emit(0x47a8)  # blx r5
    emit(0x61a0)  # retval at +18h
    emit(0x2003)
    emit(0x6020)
    branch('poll')
    if len(words) % 2: emit(0xbf00)
    pool = []
    for kind, offset, first, second in fixes:
        if kind == 'literal':
            target = len(words) * 2 + len(pool) * 4
            pool.append(second)
            displacement = address + target - ((address + offset + 4) & ~3)
            assert 0 <= displacement <= 1020 and displacement % 4 == 0
            words[offset // 2] = 0x4800 | (first << 8) | (displacement // 4)
        else:
            displacement = labels[second] - offset - 4
            assert displacement % 2 == 0
            words[offset // 2] = ((0xd000 | (first << 8) | ((displacement // 2) & 0xff))
                if first is not None else 0xe000 | ((displacement // 2) & 0x7ff))
    return struct.pack('<' + 'H' * len(words), *words) + struct.pack('<' + 'I' * len(pool), *pool)


def install(remote, thread, destination):
    allocation, thread = bootstrap_call(remote, thread, MALLOC, [0x22000, 0])
    if allocation == 0 or allocation & 3:
        raise RuntimeError('Original heap did not allocate instrumentation memory')
    code = (allocation + 0x3f) & ~0x3f
    mailbox, packet = code + 0x100, code + 0x200
    task_object, descriptor = code + 0x300, code + 0x340
    remote.write(code, task_code(code, mailbox))
    remote.write(mailbox, bytes(0x280))
    record = bytearray(28)
    # Match the original PTPSIC task's priority (0x20). A higher-priority
    # caller can starve asynchronous filesystem cleanup between commands.
    struct.pack_into('<4I', record, 0, 2, 0x20, 0x10000, 0)
    record[16:24] = b'QPTPLAB\0'
    remote.write(descriptor, record)
    result, thread = bootstrap_call(remote, thread, CREATE_TASK,
                                   [task_object, code | 1, descriptor])
    # The factory only creates a dormant task and sets its extended parameter
    # (800a). The separate original wrapper invokes sta_tsk (8003).
    started, thread = bootstrap_call(remote, thread, START_TASK, [task_object, 0])
    metadata = dict(allocation=allocation, code=code, mailbox=mailbox, packet=packet,
        task_object=task_object, descriptor=descriptor, bootstrap_thread=thread,
        return_break=code + 0x2e,
        create_result=result, start_result=started,
        task_id=int.from_bytes(remote.read(task_object, 4), 'little'),
        layer='instrumented original-firmware guest task; not physical USB')
    (destination / 'guest-task.json').write_text(json.dumps(metadata, indent=2))
    (destination / 'guest-task.bin').write_bytes(task_code(code, mailbox))
    remote.breakpoint(PTP_WAIT, False)
    return metadata


def invoke(remote, metadata, function, arguments=(), hooks=None, timeout=120):
    """Submit one native function to the genuine kernel-created lab task."""
    hooks = hooks or {}
    mailbox = metadata['mailbox']
    done = metadata.get('return_break', metadata['code'] + 0x2a)
    remote.select_thread(1)
    if 'return_break' in metadata and int.from_bytes(remote.read(mailbox + 0x1c, 4), 'little') != 1:
        faults = {}
        for thread in (1, 2):
            remote.select_thread(thread)
            faults[thread] = {name: hex(remote.read_register(name)) for name in
                             ('IFSR_S', 'IFAR_S', 'DFSR_S', 'DFAR_S')}
        raise RuntimeError('The original kernel has not entered the laboratory task: ' + repr(faults))
    state = int.from_bytes(remote.read(mailbox, 4), 'little')
    if state not in (0, 3):
        raise RuntimeError(f'Previous native request is still active: {state}')
    if len(arguments) > 4:
        raise ValueError('At most four register arguments')
    words = (1, function | 1, *arguments, *([0] * (4 - len(arguments))), 0)
    remote.write(mailbox, struct.pack('<7I', *words))
    # A completed request is stopped on the helper's branch back to poll.
    # Step that instrumentation instruction before rearming its breakpoint.
    for thread in (1, 2):
        remote.select_thread(thread)
        registers = remote.regs()
        if remote.get(registers, 'pc') == done:
            remote.command(f'vCont;s:{thread:x}')
            break
    return finish_active(remote, metadata, hooks, timeout)


def finish_active(remote, metadata, hooks=None, timeout=120):
    """Await an already submitted call; never submit or rewrite a command."""
    hooks = hooks or {}
    mailbox = metadata['mailbox']
    done = metadata.get('return_break', metadata['code'] + 0x2a)
    addresses = [done, *hooks]
    for address in addresses: remote.breakpoint(address)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            reply = continue_until(remote, deadline)
            remote.lab_stopped_thread = stopped_thread(reply)
            remote.select_thread(remote.lab_stopped_thread)
            registers = remote.regs()
            pc = remote.get(registers, 'pc')
            if pc == done:
                state = int.from_bytes(remote.read(mailbox, 4), 'little')
                if state != 3:
                    raise RuntimeError(f'Helper completion breakpoint without completed request: {state}')
                value = int.from_bytes(remote.read(mailbox + 0x18, 4), 'little')
                remote.write(mailbox, bytes(4))
                return value
            if pc in hooks:
                hooks[pc](remote, registers)
                continue
            raise RuntimeError('Original firmware stopped unexpectedly: ' + repr({
                'pc': hex(pc), 'lr': hex(remote.get(registers, 'lr')),
                'reply': reply, 'DFSR': hex(remote.read_register('DFSR_S')),
                'DFAR': hex(remote.read_register('DFAR_S'))}))
        raise TimeoutError('Original firmware request timed out')
    finally:
        for address in addresses: remote.breakpoint(address, False)


class NativePtp:
    """Original ROM handler adapter with USB-send boundary replacements.

    Firmware constructs every business response and data payload. This adapter
    consumes those bytes as a transport endpoint. It does not emulate USB
    enumeration, Windows WPD, full session observers, or hardware persistence.
    """
    RESOLVE = 0x104666a4
    SEND_RESPONSE = 0x1063f710
    RECORD_RESPONSE = 0x1063f5ec
    SEND_DATA = 0x1063f682
    SEND_BUFFERED_DATA = 0x1063f7b6
    ALLOWED = {0x1001, 0x1002, 0x1003, 0x944b, 0x94fd, 0x94fe, 0x94ff}
    SERVICES = {0xf001, 0xf002, 0xf02d, 0xf030, 0xc101, 0xc102, 0xc051, 0xc052}

    def __init__(self, remote, metadata, destination):
        self.remote = remote
        self.metadata = metadata
        self.destination = destination
        self.transaction_id = 0
        self.responses = []
        self.data = []
        self.steps = []

    def reply_boundary(self, remote, registers):
        packet = remote.get(registers, 'r0')
        if packet != self.metadata['packet']:
            raise RuntimeError('An unrelated PTP response reached the lab transport')
        outer = remote.get(registers, 'r1')
        count = remote.get(registers, 'r2')
        pointer = remote.get(registers, 'r3')
        if count > 5:
            raise RuntimeError('Invalid native response parameter count')
        params = list(struct.unpack('<' + 'I' * count, remote.read(pointer, count * 4)))
        mode = int.from_bytes(remote.read(remote.get(registers, 'sp'), 4), 'little')
        self.responses.append((outer, params))
        # Tail-call the original native response-state writer; only the USB
        # queue/send path is replaced by host consumption of the actual reply.
        remote.set(pc=self.RECORD_RESPONSE, r0=outer, r1=count, r2=pointer, r3=mode)

    def data_boundary(self, remote, registers):
        if remote.get(registers, 'r0') != self.metadata['packet']:
            raise RuntimeError('An unrelated PTP data phase reached the lab transport')
        pointer, size = remote.get(registers, 'r1'), remote.get(registers, 'r2')
        if remote.get(registers, 'pc') == self.SEND_BUFFERED_DATA:
            original_buffer = int.from_bytes(remote.read(0xc188b788, 4), 'little') + 12
            if pointer != original_buffer:
                raise RuntimeError('Native buffered transport pointer check would fail')
        if size > 0x200000:
            raise RuntimeError('Native PTP data exceeds laboratory bound')
        self.data.append(bytes(remote.read(pointer, size)))
        # The host has consumed exactly these original bytes. Return transport
        # completion, never a business authorization or commit result.
        remote.set(pc=remote.get(registers, 'lr') & ~1, r0=1)

    def observe_key_io(self, remote, registers):
        """Observe actual open/read returns; execute the original instruction."""
        pc, stack = remote.get(registers, 'pc'), remote.get(registers, 'sp')
        value = dict(pc=hex(pc), result=hex(remote.get(registers, 'r0')),
                     actual_length=int.from_bytes(remote.read(stack+0x18, 4), 'little'),
                     file_object=remote.read(stack+0x30, 40).hex())
        self.key_io.append(value)
        with (self.destination / 'key-io-observations.jsonl').open('a') as output:
            output.write(json.dumps(value) + '\n')
        remote.command(f'vCont;s:{remote.lab_stopped_thread:x}')

    def transaction(self, opcode, params):
        # Local lab control, not an on-wire opcode: continue an operation
        # that exceeded the host observation window without resubmitting it.
        if opcode == 0 and not params:
            self.remote.select_thread(1)
            active = int.from_bytes(self.remote.read(self.metadata['mailbox'], 4), 'little')
            if active not in (1, 2):
                raise RuntimeError('No submitted native operation to continue')
            cached_record = int.from_bytes(self.remote.read(0xc188bc38, 4), 'little')
            service = int.from_bytes(self.remote.read(cached_record, 2), 'little') if cached_record else None
            self.responses, self.data = [], []
            value = finish_active(self.remote, self.metadata,
                {self.SEND_RESPONSE: self.reply_boundary,
                 self.SEND_DATA: self.data_boundary,
                 self.SEND_BUFFERED_DATA: self.data_boundary}, timeout=540)
            record = dict(resumed=True, submitted_service=hex(service) if service else None,
                          return_value=value, responses=self.responses,
                          note='continued existing native call; no PTP packet or request resubmitted')
            with (self.destination / 'native-continuations.jsonl').open('a') as output:
                output.write(json.dumps(record) + '\n')
            self.transaction_id += 1
            return record
        if opcode not in self.ALLOWED or len(params) > 5:
            raise ValueError('Unsupported PTP operation')
        if opcode in (0x94fd, 0x94fe, 0x94ff) and (len(params) != 5 or params[0] not in self.SERVICES):
            raise ValueError('Only the selected original RAW service flow is admitted')
        if any(type(value) is not int or not 0 <= value <= 0xffffffff for value in params):
            raise ValueError('PTP parameters must be u32')
        packet = struct.pack('<IHHI', 12 + len(params) * 4, 1, opcode, self.transaction_id)
        packet += struct.pack('<' + 'I' * len(params), *params)
        self.remote.select_thread(1)
        active = int.from_bytes(self.remote.read(self.metadata['mailbox'], 4), 'little')
        if active not in (0, 3):
            raise RuntimeError(f'Original request remains active; packet preserved: {active}')
        self.remote.write(self.metadata['packet'], packet)
        gap = self.metadata.get('transaction_gap_ms', 0)
        if gap:
            invoke(self.remote, self.metadata, DELAY, [gap])
        self.responses, self.data = [], []
        self.key_io = []
        hooks = {self.SEND_RESPONSE: self.reply_boundary, self.SEND_DATA: self.data_boundary,
                 self.SEND_BUFFERED_DATA: self.data_boundary}
        if opcode == 0x94fd and params[0] == 0xc051:
            hooks.update({0x107791b4: self.observe_key_io,
                          0x107791d4: self.observe_key_io})
        # Deliberate handler-level integration: the full PTP observer layer
        # requires a physically connected USB-context object, absent in QEMU.
        # Resolve the original ROM record and invoke its actual callbacks.
        record_address = invoke(self.remote, self.metadata, self.RESOLVE, [opcode])
        self.remote.select_thread(1)
        fields = struct.unpack('<7I', self.remote.read(record_address, 28))
        context, prepare, execute = fields[2], fields[4], fields[5]
        prepared = (invoke(self.remote, self.metadata, prepare,
                          [context, self.metadata['packet']], hooks) if prepare else 1)
        executed = None
        if prepared:
            executed = invoke(self.remote, self.metadata, execute,
                              [context, self.metadata['packet']], hooks,
                              timeout=540 if params and params[0] == 0xf030 else 120)
        record = dict(opcode=hex(opcode), parameters=params, transaction_id=self.transaction_id,
            prepare_return=prepared, execute_return=executed,
            responses=self.responses, data_lengths=[len(data) for data in self.data],
            handler_record=hex(record_address),
            transaction_gap_ms=gap,
            key_io=self.key_io,
            layer='original ROM handler callbacks; USB/session observers bypassed; send boundary adapted')
        self.steps.append(record)
        with (self.destination / 'native-transactions.jsonl').open('a') as output:
            output.write(json.dumps(record) + '\n')
        self.transaction_id += 1
        if len(self.responses) != 1:
            raise RuntimeError('Expected one actual native response: ' + repr(record))
        return dict(data=b''.join(self.data).hex(), response=self.responses[0], evidence=record)
