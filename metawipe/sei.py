"""Neutralise encoder "user data" SEI messages in H.264/HEVC mp4/mov files, in place.

x264 and x265 write their version and complete settings as user_data_unregistered SEI messages
(payloadType 5) into the video stream, and x265 also into the SEI arrays of the `hvcC` box.
Dropping whole SEI units would also drop recovery points (cut and open-GOP files stop decoding),
frame packing and HDR10 metadata, and removing bytes would move every sample. So each type-5
message is overwritten by a reserved SEI message of exactly the same length (decoders skip
reserved payload types) filled with a constant non-zero byte. All other SEI messages and every
other byte of the file stay as they were; file size and sample offsets do not change.

The file is walked as a box tree (moov/trak/mdia/minf/stbl) and the sample table of every video
track; only `moov` and one sample at a time are held in memory.
"""
from __future__ import annotations

from bisect import bisect_right
from pathlib import Path

from .common import Context, WipeError

_AVC = {b"avc1", b"avc3", b"dva1", b"dvav"}  # dva1/dvav: Dolby Vision on an H.264 base layer
_HEVC = {b"hvc1", b"hev1", b"dvh1", b"dvhe"}
_USER_DATA = 5
_RESERVED = 0xF0  # reserved payloadType in both H.264 and HEVC: parsers skip it
_FILL = 0x55  # non-zero, so the filler can never look like a start code or need escaping
_VISUAL_ENTRY = 78  # SampleEntry + VisualSampleEntry fields before the child boxes


class _Malformed(Exception):
    pass


def _boxes(buf, start: int, end: int):
    """Yield (type, box_start, payload_start, box_end) for the boxes in buf[start:end]."""
    pos = start
    while pos + 8 <= end:
        size = int.from_bytes(buf[pos:pos + 4], "big")
        btype = bytes(buf[pos + 4:pos + 8])
        header = 8
        if size == 1:
            size = int.from_bytes(buf[pos + 8:pos + 16], "big")
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            raise WipeError("corrupt video: bad box size")
        yield btype, pos, pos + header, pos + size
        pos += size


def _file_boxes(f, file_size: int):
    """Top-level boxes, read header by header (mdat is never loaded)."""
    pos = 0
    while pos + 8 <= file_size:
        f.seek(pos)
        head = f.read(16)
        size, btype, header = int.from_bytes(head[:4], "big"), head[4:8], 8
        if size == 1:
            size, header = int.from_bytes(head[8:16], "big"), 16
        elif size == 0:
            size = file_size - pos
        if size < header or pos + size > file_size:
            raise WipeError("corrupt video: bad top-level box size")
        yield btype, pos, pos + header, pos + size
        pos += size


def _unescape(nal, start: int):
    """RBSP of nal[start:] (emulation prevention bytes removed) and a function rbsp index -> nal index."""
    epb = []  # rbsp index of the byte that follows each removed 0x03
    rbsp = bytearray()
    pos = start
    hit = nal.find(b"\x00\x00\x03", pos)
    while hit != -1:
        rbsp += nal[pos:hit + 2]
        epb.append(len(rbsp))
        pos = hit + 3
        hit = nal.find(b"\x00\x00\x03", pos)
    rbsp += nal[pos:]
    return rbsp, lambda i: start + i + bisect_right(epb, i)


def _messages(rbsp):
    """Yield (type_start, payload_type, payload_start, payload_end) for the SEI messages in rbsp."""
    stop = len(rbsp)
    while stop and rbsp[stop - 1] == 0:
        stop -= 1
    if not stop or rbsp[stop - 1] != 0x80:
        raise _Malformed  # SEI messages are byte aligned, so the RBSP must end with the 0x80 stop byte
    stop -= 1
    p = 0
    while p < stop:
        start = p
        values = []
        for _ in range(2):  # payloadType, payloadSize: runs of 0xFF plus a last byte
            value = 0
            while p < stop and rbsp[p] == 0xFF:
                value += 255
                p += 1
            if p >= stop:
                raise _Malformed
            values.append(value + rbsp[p])
            p += 1
        ptype, psize = values
        if p + psize > stop:
            raise _Malformed
        yield start, ptype, p, p + psize
        p += psize


def _reserved_message(n: int):
    """One reserved SEI message of exactly n bytes with a non-empty payload, or None."""
    for size in range(max(1, n - 2 - n // 255 - 1), n - 1):
        coded = b"\xff" * (size // 255) + bytes([size % 255])
        if 1 + len(coded) + size == n:
            return bytes([_RESERVED]) + coded + bytes([_FILL]) * size
    return None


def _filler(n: int) -> bytes:
    """n bytes (n >= 3) of reserved SEI messages, ending in a non-zero byte."""
    one = _reserved_message(n)
    if one is None:  # n = 256k + 1 has no single-message encoding; 3 + (n - 3) always has one
        one = _reserved_message(3) + _reserved_message(n - 3)
    return one


def scrub_nal(nal: bytearray, header: int) -> int:
    """Overwrite the user_data_unregistered messages of one SEI NAL unit in place; returns how many.

    The replaced range runs from the payloadType byte to the next RBSP byte, so an emulation
    prevention byte inside the payload or right after it is replaced too; the filler needs none
    and ends in a non-zero byte, so the bytes before and after it keep their meaning.
    """
    rbsp, raw = _unescape(nal, header)
    regions = [(raw(start), raw(end)) for start, ptype, _, end in _messages(rbsp) if ptype == _USER_DATA and end - start > 2]
    for a, b in regions:
        nal[a:b] = _filler(b - a)
    if regions:  # the result must parse into the same layout, with no user data left
        rbsp, _ = _unescape(nal, header)
        if any(ptype == _USER_DATA for _, ptype, _, _ in _messages(rbsp)):
            raise _Malformed
    return len(regions)


def _is_sei(first: int, hevc: bool) -> bool:
    """Whether a NAL unit whose first byte is `first` is an SEI unit (HEVC: prefix or suffix SEI)."""
    return (first >> 1) & 0x3F in (39, 40) if hevc else first & 0x1F == 6


def _scrub_array_nals(buf: bytearray, p: int, end: int, n_arrays: int) -> int:
    """SEI NAL units inside the parameter set arrays of an hvcC/lhvC box."""
    count = 0
    for _ in range(n_arrays):
        if p + 3 > end:
            raise WipeError("corrupt video: hvcC array overruns its box")
        nal_type, n_nals = buf[p] & 0x3F, int.from_bytes(buf[p + 1:p + 3], "big")
        p += 3
        for _ in range(n_nals):
            length = int.from_bytes(buf[p:p + 2], "big")
            p += 2
            if p + length > end:
                raise WipeError("corrupt video: hvcC NAL unit overruns its box")
            if nal_type in (39, 40) and length > 2 and _is_sei(buf[p], True):
                nal = bytearray(buf[p:p + length])
                try:
                    count += scrub_nal(nal, 2)
                except _Malformed:
                    raise WipeError("corrupt video: malformed SEI in hvcC") from None
                buf[p:p + length] = nal
            p += length
    return count


def _sample_entry(buf: bytearray, entry: tuple):
    """(codec, NAL length size, user data removed from the config box) for a sample entry, or None."""
    btype, _, payload, end = entry
    hevc = btype in _HEVC
    if not hevc and btype not in _AVC:
        return None
    length_size, removed = None, 0
    for ctype, _, cp, ce in _boxes(buf, payload + _VISUAL_ENTRY, end):
        if ctype == b"avcC" and not hevc and ce - cp >= 7:
            length_size = (buf[cp + 4] & 3) + 1  # avcC holds SPS/PPS/SPS-ext only, never SEI
        elif ctype == b"hvcC" and hevc and ce - cp >= 23:
            length_size = (buf[cp + 21] & 3) + 1
            removed += _scrub_array_nals(buf, cp + 23, ce, buf[cp + 22])
        elif ctype == b"lhvC" and hevc and ce - cp >= 6:  # layered HEVC (e.g. MV-HEVC spatial video)
            removed += _scrub_array_nals(buf, cp + 6, ce, buf[cp + 5])
    if length_size is None or length_size == 3:
        raise WipeError(f"corrupt video: {btype.decode('latin-1')} sample entry without a usable configuration box")
    return hevc, length_size, removed


def _u32(buf, pos: int) -> int:
    return int.from_bytes(buf[pos:pos + 4], "big")


def _sample_sizes(buf: bytearray, boxes: dict) -> list:
    if b"stsz" in boxes:
        p = boxes[b"stsz"][2]
        fixed, count = _u32(buf, p + 4), _u32(buf, p + 8)
        if fixed:
            if count > 1 << 26:  # a corrupt count must not allocate gigabytes
                raise WipeError("corrupt video: absurd sample count")
            return [fixed] * count
        if p + 12 + 4 * count > boxes[b"stsz"][3]:
            raise WipeError("corrupt video: stsz overruns its box")
        return [_u32(buf, p + 12 + 4 * k) for k in range(count)]
    if b"stz2" in boxes:
        p = boxes[b"stz2"][2]
        bits, count = buf[p + 7], _u32(buf, p + 8)
        if bits not in (4, 8, 16) or p + 12 + (count * bits + 7) // 8 > boxes[b"stz2"][3]:
            raise WipeError("corrupt video: bad stz2 box")
        if bits == 4:
            return [(buf[p + 12 + k // 2] >> (0 if k % 2 else 4)) & 15 for k in range(count)]
        step = bits // 8
        return [int.from_bytes(buf[p + 12 + step * k:p + 12 + step * (k + 1)], "big") for k in range(count)]
    raise WipeError("corrupt video: track without sample sizes")


def _samples(buf: bytearray, boxes: dict):
    """Yield (offset, size, sample_description_index) for every sample of a track."""
    sizes = _sample_sizes(buf, boxes)
    if b"stco" in boxes:
        key, width = b"stco", 4
    elif b"co64" in boxes:
        key, width = b"co64", 8
    else:
        raise WipeError("corrupt video: track without chunk offsets")
    p, end = boxes[key][2], boxes[key][3]
    n_chunks = _u32(buf, p + 4)
    if p + 8 + width * n_chunks > end:
        raise WipeError("corrupt video: chunk offset table overruns its box")
    chunks = [int.from_bytes(buf[p + 8 + width * k:p + 8 + width * (k + 1)], "big") for k in range(n_chunks)]
    if b"stsc" not in boxes:
        raise WipeError("corrupt video: track without sample-to-chunk table")
    p, end = boxes[b"stsc"][2], boxes[b"stsc"][3]
    n_runs = _u32(buf, p + 4)
    if p + 8 + 12 * n_runs > end:
        raise WipeError("corrupt video: stsc overruns its box")
    runs = [(_u32(buf, p + 8 + 12 * k), _u32(buf, p + 12 + 12 * k), _u32(buf, p + 16 + 12 * k)) for k in range(n_runs)]
    sample = 0
    for r, (first, per_chunk, sdi) in enumerate(runs):
        last = runs[r + 1][0] - 1 if r + 1 < len(runs) else n_chunks
        if first < 1 or last > n_chunks:
            raise WipeError("corrupt video: stsc refers to missing chunks")
        for chunk in range(first, last + 1):
            offset = chunks[chunk - 1]
            for _ in range(per_chunk):
                if sample >= len(sizes):
                    raise WipeError("corrupt video: sample tables disagree")
                yield offset, sizes[sample], sdi
                offset += sizes[sample]
                sample += 1
    if sample != len(sizes):
        raise WipeError("corrupt video: sample tables disagree")


def _scrub_samples(f, file_size: int, samples, entries: dict, n_entries: int) -> tuple:
    """Walk the length-prefixed NAL units of every sample; returns (messages removed, unparsable samples)."""
    removed = bad = 0
    for offset, size, sdi in samples:
        if sdi not in entries:
            if not 1 <= sdi <= n_entries:
                raise WipeError("corrupt video: sample refers to a missing sample description")
            continue  # a sample entry of another codec, reported by the caller
        hevc, length_size = entries[sdi]
        header = 2 if hevc else 1
        if offset + size > file_size:
            raise WipeError("corrupt video: sample outside the file")
        f.seek(offset)
        data = f.read(size)
        pos = 0
        while pos < size:
            n = int.from_bytes(data[pos:pos + length_size], "big")
            pos += length_size
            if pos + n > size:
                bad += 1
                break
            if n > header and _is_sei(data[pos], hevc):
                nal = bytearray(data[pos:pos + n])
                try:
                    count = scrub_nal(nal, header)
                except _Malformed:
                    bad += 1
                    count = 0
                if count:
                    f.seek(offset + pos)
                    f.write(nal)
                    removed += count
            pos += n
    return removed, bad


def _child(buf, start: int, end: int, wanted: bytes):
    return next((b for b in _boxes(buf, start, end) if b[0] == wanted), None)


def scrub(path: Path, ctx: Context) -> int:
    """Neutralise user-data SEI in every H.264/HEVC track of path; returns the number of messages removed."""
    removed = 0
    with open(path, "r+b") as f:
        f.seek(0, 2)
        file_size = f.tell()
        top = list(_file_boxes(f, file_size))
        kinds = [b[0] for b in top]
        if kinds.count(b"moov") != 1:
            raise WipeError("corrupt video: expected exactly one moov box")
        if b"moof" in kinds:
            raise WipeError("fragmented MP4 is not supported for --full")
        _, _, moov_payload, moov_end = top[kinds.index(b"moov")]
        f.seek(moov_payload)
        moov = bytearray(f.read(moov_end - moov_payload))
        original = bytes(moov)
        tracks = []
        for btype, _, payload, end in _boxes(moov, 0, len(moov)):
            if btype == b"mvex":
                raise WipeError("fragmented MP4 is not supported for --full")
            if btype == b"trak":
                tracks.append((payload, end))
        for number, (payload, end) in enumerate(tracks, 1):
            mdia = _child(moov, payload, end, b"mdia")
            hdlr = mdia and _child(moov, mdia[2], mdia[3], b"hdlr")
            minf = mdia and _child(moov, mdia[2], mdia[3], b"minf")
            stbl = minf and _child(moov, minf[2], minf[3], b"stbl")
            if not stbl:
                continue
            handler = bytes(moov[hdlr[2] + 8:hdlr[2] + 12]) if hdlr else b""
            table = {}
            for box in _boxes(moov, stbl[2], stbl[3]):
                table.setdefault(box[0], box)
            if b"stsd" not in table:
                continue
            entries, unsupported = {}, []
            stsd = list(_boxes(moov, table[b"stsd"][2] + 8, table[b"stsd"][3]))
            for index, entry in enumerate(stsd, 1):
                info = _sample_entry(moov, entry)
                if info is None:
                    unsupported.append(entry[0].decode("latin-1").strip())
                    continue
                entries[index] = info[:2]
                removed += info[2]
            if handler == b"vide" and unsupported:
                what = "encrypted" if "encv" in unsupported else "/".join(unsupported)
                ctx.leftover(f"video track {number} ({what}): encoder strings inside the bitstream cannot be removed")
            if not entries:
                continue
            count, bad = _scrub_samples(f, file_size, _samples(moov, table), entries, len(stsd))
            removed += count
            if bad:
                ctx.leftover(f"video track {number}: {bad} sample(s) could not be parsed; encoder strings may remain there")
        if moov != original:
            f.seek(moov_payload)
            f.write(moov)
    return removed
