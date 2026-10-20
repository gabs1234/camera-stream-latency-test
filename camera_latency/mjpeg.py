"""Incremental Axis multipart parser; HTTP transport is delegated to HTTPX."""
from email.message import Message

MAX_JPEG = 32 * 1024 * 1024
MAX_HEADERS = 16 * 1024


def boundary_from_content_type(content_type: str) -> bytes:
    msg = Message()
    msg['content-type'] = content_type
    boundary = msg.get_param('boundary')
    if msg.get_content_type() != 'multipart/x-mixed-replace' or not boundary:
        raise ValueError('expected multipart/x-mixed-replace with boundary')
    result = boundary.encode('ascii')
    if not 1 <= len(result) <= 200 or b'\r' in result or b'\n' in result:
        raise ValueError('invalid multipart boundary')
    return b'--' + result


class MultipartJPEG:
    """Yield as soon as Content-Length bytes exist, without awaiting next boundary.

    Missing Content-Length is rejected: Axis documents it, and guessing JPEG ends
    or waiting for another frame would give different availability semantics.
    """
    def __init__(self, boundary: bytes):
        self.boundary = boundary
        self.buffer = bytearray()
        self.length = None

    def feed(self, chunk: bytes):
        self.buffer.extend(chunk)
        while True:
            if self.length is None:
                pos = self.buffer.find(self.boundary)
                if pos < 0:
                    if len(self.buffer) > MAX_HEADERS:
                        raise ValueError('multipart boundary missing')
                    return
                end = self.buffer.find(b'\r\n\r\n', pos + len(self.boundary))
                if end < 0:
                    if len(self.buffer) > MAX_HEADERS:
                        raise ValueError('oversized multipart headers')
                    return
                headers = Message()
                for line in bytes(self.buffer[pos + len(self.boundary):end]).split(b'\r\n'):
                    if b':' in line:
                        k, v = line.split(b':', 1)
                        headers[k.decode('ascii')] = v.decode('ascii').strip()
                if headers.get('Content-Type', '').lower() != 'image/jpeg':
                    raise ValueError('expected image/jpeg part')
                try:
                    self.length = int(headers['Content-Length'])
                except (TypeError, ValueError):
                    raise ValueError('Axis MJPEG requires Content-Length per part') from None
                if not 1 <= self.length <= MAX_JPEG:
                    raise ValueError('JPEG size outside safety limit')
                del self.buffer[:end + 4]
            if len(self.buffer) < self.length:
                return
            jpeg = bytes(self.buffer[:self.length])
            del self.buffer[:self.length]
            self.length = None
            yield jpeg
