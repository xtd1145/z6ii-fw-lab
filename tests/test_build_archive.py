"""Ensure QEMU's internal header links survive extraction on supported Python."""
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.build import source_filter


class ArchiveLinks(unittest.TestCase):
    def test_internal_qemu_header_link_is_preserved(self):
        member = tarfile.TarInfo('qemu/subprojects/libvhost-user/include/atomic.h')
        member.type = tarfile.SYMTYPE
        member.linkname = '../../../include/qemu/atomic.h'
        with tempfile.TemporaryDirectory() as root:
            filtered = source_filter(member, root)
        self.assertEqual(filtered.linkname, member.linkname)
        self.assertTrue(filtered.issym())

    def test_external_and_absolute_links_are_skipped(self):
        for target in ('../../outside', '/usr/include/stdint.h'):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as root:
                member = tarfile.TarInfo('qemu/header')
                member.type = tarfile.SYMTYPE
                member.linkname = target
                self.assertIsNone(source_filter(member, root))

    def test_member_path_cannot_escape(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(tarfile.OutsideDestinationError):
                source_filter(tarfile.TarInfo('../outside'), root)


if __name__ == '__main__':
    unittest.main()
