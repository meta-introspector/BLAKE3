"""code_hash.py's contract: python3 -m unittest discover -s tools/mutants.

An executable's hash covers every byte the loader maps and nothing else:
a byte changed in a mapped section changes it, one changed in a section
the loader skips leaves it, so a mutant whose build hashes as the
unmutated one's runs the same (check.py's "same code")."""

import os
import shutil
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import code_hash  # noqa: E402

ELF = shutil.which("true")


def sections(path):
    """(name, flags, offset, size) of each section with bytes in the file."""
    data = open(path, "rb").read()
    shoff, = struct.unpack_from("<Q", data, 0x28)
    shentsize, shnum, shstrndx = struct.unpack_from("<HHH", data, 0x3A)
    head = lambda i: struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * shentsize)
    names = data[head(shstrndx)[4]:]
    out = []
    for i in range(shnum):
        name, kind, flags, _, offset, size = head(i)[:6]
        if kind != code_hash.SHT_NOBITS and size:
            out.append((names[name:names.index(b"\0", name)].decode(), flags, offset, size))
    return out


@unittest.skipUnless(sys.platform == "linux" and ELF and os.environ.get("MUTANTS_B3SUM"),
                     "an ELF executable to read, and MUTANTS_B3SUM: official b3sum 1.8.7 (check.py's official_b3sum installs it)")
class CodeHash(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def flipped(self, pick):
        """A copy of ELF with one byte flipped in the first section `pick`
        accepts, and its hash."""
        path = os.path.join(self.dir, "copy")
        shutil.copy(ELF, path)
        name, _, offset, size = next(s for s in sections(path) if pick(s))
        data = bytearray(open(path, "rb").read())
        data[offset + size // 2] ^= 0xFF
        open(path, "wb").write(data)
        return name, code_hash.code_hash(path)

    def test_a_copy_hashes_alike(self):
        path = os.path.join(self.dir, "copy")
        shutil.copy(ELF, path)
        self.assertEqual(code_hash.code_hash(path), code_hash.code_hash(ELF))

    def test_a_mapped_byte_changes_the_hash(self):
        name, h = self.flipped(lambda s: s[0] == ".text")
        self.assertNotEqual(h, code_hash.code_hash(ELF), name)

    def test_an_unmapped_byte_leaves_it(self):
        name, h = self.flipped(lambda s: not s[1] & code_hash.SHF_ALLOC and s[0] != ".shstrtab")
        self.assertEqual(h, code_hash.code_hash(ELF), name)


if __name__ == "__main__":
    unittest.main()
