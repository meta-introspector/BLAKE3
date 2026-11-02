#!/usr/bin/env python3
"""Generate c/blake3_avx512_32.S: pure-assembly wide AVX-512 kernels.

C ABI, matching the AArch64 kernel family:
  (inputs, blocks, key, counter, packed_flags, out)
  inputs: 32 readable pointers for k32/p32, 16 for p16, each to
  blocks*64 readable bytes; blocks: 1..=16; key: eight u32 words;
  out: 32 bytes per input;
  packed_flags = flags | start << 8 | end << 16 | last_len << 24.
  last_len is 0..=64, recorded on the final block, which is read as 64
  bytes and arrives zero-padded when short. All inputs are read before
  any output is written, so input/output overlap is permitted.
  k32 increments the u64 counter per input, wrapping; p32/p16 keep it fixed.
  Linux x86-64 SysV ABI; AVX-512F. No calls, no heap allocation.

32-input register layout:
  CVs: ZMM0..7 for group A, ZMM8..15 for group B.
  Upper state: ZMM16..23 for group A, ZMM24..31 for group B.
  Each register holds one word across 16 inputs, in ascending input order.

The transpose uses 17 registers, not two banks of 16: emit the low result
into the scratch register, then the high result over the first input.
The second input's register becomes the next scratch. Register identities
rotate at generation time; there are no register-copy instructions in the
transpose. Each butterfly still costs just two instructions. This leaves
15 CVs live during transposition, with only ZMM15 saved and restored per
block. The seven rounds and feed-forward keep all state in registers. One round
is emitted and repeated seven times, with message offsets read from a
constant table. This experiment tests the cost of extra offset loads
against the smaller instruction footprint on calls after other work.

One flat block loop; no transposition batch size to select. The stack holds
2 KiB of transposed messages, 256 bytes of counter vectors and one 64-byte
CV spill, 2368 bytes total. Nine-register output transposition packs two
CVs per store without zero rows or group-B copies. The frame is aligned
to 64 bytes; its allocation plus
alignment and saved GPRs is less than a 4 KiB page. The CFA stays on rbp.

The p16 experiment keeps both state and messages in registers. A two-round
body repeats three times, then its first round runs once more. Between
pairs, PERM squared rotates four disjoint four-cycles of message registers,
using four 64-byte scratch slots to break the cycles. Its 256-byte frame
has no message buffer or counter vectors. CVs stay in ZMM0..7 during the
input transpose; its other live registers determine the upper-state map.
It has no input prefetches; production use is being tested for parents only.

Start/end flags apply only to the first/last block (both for one block),
and last_len only to the final block. The 32-input prefetches look three blocks ahead,
as in the existing 16-way kernel; these hints perform no architectural
reads. The contract above is what tools/verify/prove_x86.py proves (its
"wide" cases: each kernel at 1 to 16 blocks, for every input).

    python3 tools/gen_avx512_32.py > c/blake3_avx512_32.S
"""
IV = [0x6A09E667, 0xBB67AE85, 0x3C6EF372, 0xA54FF53A]
PERM = [2, 6, 3, 10, 7, 0, 4, 13, 1, 11, 12, 5, 9, 14, 15, 8]
COLS = [(0,4,8,12), (1,5,9,13), (2,6,10,14), (3,7,11,15)]
DIAGS = [(0,5,10,15), (1,6,11,12), (2,7,8,13), (3,4,9,14)]
WORD = [0,2,1,3,4,6,5,7,8,10,9,11,12,14,13,15]
INDEX = [('rax','eax'), ('rcx','ecx'), ('rdx','edx'), ('r8','r8d')]
COUNTERS = 2048
SPILL = COUNTERS + 256
FRAME = SPILL + 64
assert FRAME % 64 == 0 and FRAME + 64 + 48 < 4096


def emit(s):
    print('    ' + s)


def state(group, word):
    """Both CV rows in the low sixteen registers; both upper rows above."""
    return 8*group+word if word < 8 else 16+8*group+(word-8)


def transpose(rows, scratch):
    """Eight or sixteen rows, with one rotating scratch register."""
    rows = rows.copy()
    n = len(rows)
    assert n in (8,16)
    registers = set(rows + [scratch])
    assert len(registers) == n+1
    for distance,lo,hi,imm_lo,imm_hi in [
        (1,'vpunpckldq','vpunpckhdq','',''),
        (2,'vpunpcklqdq','vpunpckhqdq','',''),
        (4,'vshufi32x4','vshufi32x4',', 0x88',', 0xdd'),
        (8,'vshufi32x4','vshufi32x4',', 0x88',', 0xdd')]:
        if distance >= n:
            break
        for base in range(0,n,2*distance):
            for i in range(base,base+distance):
                j = i+distance
                a,b = rows[i],rows[j]
                emit(f'{lo} zmm{scratch}, zmm{a}, zmm{b}{imm_lo}')
                emit(f'{hi} zmm{a}, zmm{a}, zmm{b}{imm_hi}')
                rows[i],rows[j],scratch = scratch,a,b
                assert set(rows+[scratch]) == registers and len(set(rows)) == n
    return rows


def output_group(first, offset):
    """Eight CV words across sixteen lanes -> sixteen contiguous CVs."""
    original = list(range(first, first+8))
    rows = transpose(original,16)
    unused = set(original+[16])-set(rows)
    assert len(unused) == 1
    scratch = unused.pop()
    for high, imm in enumerate((0x88,0xdd)):
        for pair in range(4):
            a,b = rows[WORD[2*pair]],rows[WORD[2*pair+1]]
            emit(f'vshufi32x4 zmm{scratch}, zmm{a}, zmm{b}, {imm}')
            emit(f'vmovdqu32 ZMMWORD PTR [r15 + {offset+64*(4*high+pair)}], zmm{scratch}')


def g(a, b, c, d, x, y):
    return [f'vpaddd {a}, {a}, {x}', f'vpaddd {a}, {a}, {b}',
            f'vpxord {d}, {d}, {a}', f'vprord {d}, {d}, 16',
            f'vpaddd {c}, {c}, {d}', f'vpxord {b}, {b}, {c}', f'vprord {b}, {b}, 12',
            f'vpaddd {a}, {a}, {y}', f'vpaddd {a}, {a}, {b}',
            f'vpxord {d}, {d}, {a}', f'vprord {d}, {d}, 8',
            f'vpaddd {c}, {c}, {d}', f'vpxord {b}, {b}, {c}', f'vprord {b}, {b}, 7']


def g_chain(k, positions, index):
    registers = [f'zmm{state(k,w)}' for w in positions]
    message = f'ZMMWORD PTR [rsi + {INDEX[index][0]} + {1024*k}]'
    return g(*registers, message, message)


def parent16():
    """Fixed-counter kernel, with register messages and a two-round loop."""
    p2 = [PERM[PERM[i]] for i in range(16)]
    assert sorted(p2) == list(range(16))
    cycles, seen = [], set()
    for start in range(16):
        if start in seen:
            continue
        cycle, word = [], start
        while word not in seen:
            seen.add(word)
            cycle.append(word)
            word = p2[word]
        assert word == start
        cycles.append(cycle)
    assert len(cycles) == 4 and all(len(c) == 4 for c in cycles)
    frame = 64 * len(cycles)
    assert frame + 64 + 48 < 4096
    name = 'blake3_servil_avx512_p16'
    print(f'.p2align 6\n.global {name}\n.hidden {name}\n.type {name}, @function\n{name}:\n.cfi_startproc')
    emit('endbr64')
    emit('push rbp')
    print('.cfi_def_cfa_offset 16\n.cfi_offset rbp, -16')
    emit('mov rbp, rsp')
    print('.cfi_def_cfa_register rbp')
    for i, reg in enumerate(('rbx','r12','r13','r14','r15')):
        emit(f'push {reg}')
        print(f'.cfi_offset {reg}, {-24-8*i}')
    emit('and rsp, -64')
    emit(f'sub rsp, {frame}')
    emit('mov BYTE PTR [rsp], 0')
    for dst, src in [('r12','rdi'),('r13','rsi'),('r14','rdx'),('r15','r9'),('rbx','r8'),('r9','rcx')]:
        emit(f'mov {dst}, {src}')
    for w in range(8):
        emit(f'vpbroadcastd zmm{w}, DWORD PTR [r14 + {4*w}]')
    emit('xor r14d, r14d')
    emit('movzx r8d, bl')
    emit('mov r10d, ebx')
    emit('shr r10d, 8')
    emit('and r10d, 255')
    emit('or r10d, r8d')
    emit('mov edi, ebx')
    emit('shr edi, 16')
    emit('and edi, 255')
    emit('mov esi, ebx')
    emit('shr esi, 24')
    print('.Lblock16:')
    for i in range(16):
        emit(f'mov rax, QWORD PTR [r12 + {8*i}]')
        emit(f'vmovdqu32 zmm{16+i}, ZMMWORD PTR [rax + r14]')
    rows = transpose(list(range(16,32)), 8)
    messages = [rows[WORD[w]] for w in range(16)]
    upper = [r for r in range(8,32) if r not in messages]
    registers = list(range(8)) + upper
    assert len(upper) == 8 and len(set(registers + messages)) == 32
    emit('mov eax, r10d')
    emit('mov edx, 64')
    emit('cmp r13, 1')
    emit('jne .Lflags16')
    emit('or eax, edi')
    emit('mov edx, esi')
    print('.Lflags16:')
    for w in range(4):
        emit(f'vpbroadcastd zmm{registers[8+w]}, DWORD PTR [rip + .Liv32 + {4*w}]')
    emit(f'vpbroadcastd zmm{registers[12]}, r9d')
    emit('mov rcx, r9')
    emit('shr rcx, 32')
    emit(f'vpbroadcastd zmm{registers[13]}, ecx')
    emit(f'vpbroadcastd zmm{registers[14]}, edx')
    emit(f'vpbroadcastd zmm{registers[15]}, eax')
    emit('mov r11d, 7')
    print('.p2align 5\n.Lround16:')
    for permutation in (list(range(16)), PERM):
        for half, positions in enumerate((COLS, DIAGS)):
            chains = [g(*[f'zmm{registers[w]}' for w in pos],
                        f'zmm{messages[permutation[8*half+2*i]]}',
                        f'zmm{messages[permutation[8*half+2*i+1]]}')
                      for i, pos in enumerate(positions)]
            for instructions in zip(*chains):
                for instruction in instructions:
                    emit(instruction)
        emit('dec r11d')
        if permutation == list(range(16)):
            emit('jz .Lround_done16')
    # New message[i] = old message[PERM squared[i]]. Each last load feeds
    # a late G operand (not the next round's first column-message adds).
    symbolic = list(range(16))
    for slot, cycle in enumerate(cycles):
        saved = symbolic[cycle[0]]
        emit(f'vmovdqa32 ZMMWORD PTR [rsp + {64*slot}], zmm{messages[cycle[0]]}')
        for dst, src in zip(cycle, cycle[1:]):
            emit(f'vmovdqa32 zmm{messages[dst]}, zmm{messages[src]}')
            symbolic[dst] = symbolic[src]
        emit(f'vmovdqa32 zmm{messages[cycle[-1]]}, ZMMWORD PTR [rsp + {64*slot}]')
        symbolic[cycle[-1]] = saved
    assert symbolic == p2
    emit('jmp .Lround16')
    print('.Lround_done16:')
    for w in range(8):
        emit(f'vpxord zmm{w}, zmm{w}, zmm{registers[8+w]}')
    emit('mov r10d, r8d')
    emit('add r14, 64')
    emit('dec r13')
    emit('jne .Lblock16')
    output_group(0, 0)
    emit('vzeroupper')
    emit('lea rsp, [rbp - 40]')
    for reg in ('r15','r14','r13','r12','rbx'):
        emit(f'pop {reg}')
        print(f'.cfi_restore {reg}')
    emit('pop rbp')
    print('.cfi_def_cfa rsp, 8\n.cfi_restore rbp')
    emit('ret')
    print(f'.cfi_endproc\n.size {name}, .-{name}')


print('// Generated by tools/gen_avx512_32.py; contracts and layout are in its header.')
print('.intel_syntax noprefix\n.text\n.p2align 6')
for name in ('blake3_servil_avx512_k32','blake3_servil_avx512_p32'):
    print(f'.global {name}\n.hidden {name}\n.type {name}, @function')
print('blake3_servil_avx512_k32:\n.cfi_startproc')
emit('endbr64')
emit('mov r10d, 1')
emit('jmp .Lenter32')
print('blake3_servil_avx512_p32:')
emit('endbr64')
emit('xor r10d, r10d')
print('.Lenter32:')
emit('push rbp')
print('.cfi_def_cfa_offset 16\n.cfi_offset rbp, -16')
emit('mov rbp, rsp')
print('.cfi_def_cfa_register rbp')
for i,reg in enumerate(('rbx','r12','r13','r14','r15')):
    emit(f'push {reg}')
    print(f'.cfi_offset {reg}, {-24-8*i}')
emit('and rsp, -64')
emit(f'sub rsp, {FRAME}')
emit('mov BYTE PTR [rsp], 0')
for dst,src in [('r12','rdi'),('r13','rsi'),('r14','rdx'),('r15','r9'),('rbx','r8')]:
    emit(f'mov {dst}, {src}')
# Counter vectors, before loading CVs into the low registers.
emit('vpbroadcastd zmm0, ecx')
emit('shr rcx, 32')
emit('vpbroadcastd zmm1, ecx')
emit('vmovdqa32 zmm2, zmm0')
emit('vmovdqa32 zmm3, zmm0')
emit('vmovdqa32 zmm5, zmm1')
emit('vmovdqa32 zmm6, zmm1')
emit('test r10d, r10d')
emit('jz .Lcounter32')
emit('vpaddd zmm2, zmm0, ZMMWORD PTR [rip + .Lindices32]')
emit('vpaddd zmm3, zmm0, ZMMWORD PTR [rip + .Lindices32 + 64]')
emit('vpcmpud k1, zmm2, zmm0, 1')
emit('vpcmpud k2, zmm3, zmm0, 1')
emit('vpbroadcastd zmm4, r10d')
emit('vpaddd zmm5 {k1}, zmm1, zmm4')
emit('vpaddd zmm6 {k2}, zmm1, zmm4')
print('.Lcounter32:')
for off,reg in enumerate((2,5,3,6)):
    emit(f'vmovdqa32 ZMMWORD PTR [rsp + {COUNTERS+64*off}], zmm{reg}')
for k in range(2):
    for w in range(8):
        emit(f'vpbroadcastd zmm{state(k,w)}, DWORD PTR [r14 + {4*w}]')
emit('xor r14d, r14d')  # Byte offset into every input.
emit('mov rsi, rsp')
print('.Lblock32:')
emit(f'vmovdqa32 ZMMWORD PTR [rsp + {SPILL}], zmm15')
for k in range(2):
    for i in range(16):
        emit(f'mov rax, QWORD PTR [r12 + {8*(16*k+i)}]')
        emit(f'vmovdqu32 zmm{16+i}, ZMMWORD PTR [rax + r14]')
        emit('prefetcht0 BYTE PTR [rax + r14 + 192]')
    rows = transpose(list(range(16,32)),15)
    for w in range(16):
        emit(f'vmovdqa32 ZMMWORD PTR [rsi + {64*(16*k+w)}], zmm{rows[WORD[w]]}')
emit(f'vmovdqa32 zmm15, ZMMWORD PTR [rsp + {SPILL}]')
emit('movzx eax, bl')
emit('test r14, r14')
emit('jnz .Lstart32')
emit('mov edx, ebx')
emit('shr edx, 8')
emit('and edx, 255')
emit('or eax, edx')
print('.Lstart32:')
emit('mov edx, 64')
emit('cmp r13, 1')
emit('jne .Lflags32')
emit('mov ecx, ebx')
emit('shr ecx, 16')
emit('and ecx, 255')
emit('or eax, ecx')
emit('mov edx, ebx')
emit('shr edx, 24')
print('.Lflags32:')
for k in range(2):
    for w in range(4):
        emit(f'vpbroadcastd zmm{state(k,8+w)}, DWORD PTR [rip + .Liv32 + {4*w}]')
    for w in range(2):
        emit(f'vmovdqa32 zmm{state(k,12+w)}, ZMMWORD PTR [rsp + {COUNTERS+64*(2*k+w)}]')
    emit(f'vpbroadcastd zmm{state(k,14)}, edx')
    emit(f'vpbroadcastd zmm{state(k,15)}, eax')
emit('lea rdi, [rip + .Lschedule32]')
emit('mov r11d, 7')
print('.p2align 5\n.Lround32:')
for half,positions in enumerate((COLS,DIAGS)):
    chains = [g_chain(k,pos,i) for k in range(2) for i,pos in enumerate(positions)]
    for step,instructions in enumerate(zip(*chains)):
        if step in (0,7):
            for i,(_,reg32) in enumerate(INDEX):
                offset = 4*(8*half+2*i+(step == 7))
                emit(f'mov {reg32}, DWORD PTR [rdi + {offset}]')
        for instruction in instructions:
            emit(instruction)
emit('add rdi, 64')
emit('dec r11d')
emit('jne .Lround32')
for k in range(2):
    for w in range(8):
        emit(f'vpxord zmm{state(k,w)}, zmm{state(k,w)}, zmm{state(k,8+w)}')
emit('add r14, 64')
emit('dec r13')
emit('jne .Lblock32')
for k in range(2):
    output_group(8*k, 512*k)
emit('vzeroupper')
emit('lea rsp, [rbp - 40]')
for reg in ('r15','r14','r13','r12','rbx'):
    emit(f'pop {reg}')
    print(f'.cfi_restore {reg}')
emit('pop rbp')
print('.cfi_def_cfa rsp, 8\n.cfi_restore rbp')
emit('ret')
print('.cfi_endproc')
for name in ('blake3_servil_avx512_k32','blake3_servil_avx512_p32'):
    print(f'.size {name}, .-{name}')
parent16()
print('.section .rodata\n.p2align 6\n.Liv32:\n.long ' + ', '.join(hex(w) for w in IV))
print('.p2align 6\n.Lindices32:\n.long ' + ', '.join(str(i) for i in range(32)))
print('.p2align 6\n.Lschedule32:')
schedule = list(range(16))
for _ in range(7):
    print('.long ' + ', '.join(str(64*w) for w in schedule))
    schedule = [schedule[p] for p in PERM]
print('.section .note.GNU-stack,"",@progbits')
