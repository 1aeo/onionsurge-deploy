"""Optional installed-rclone integration against a loopback S3 fixture only."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import publication as pub


@unittest.skipUnless(os.environ.get('ONIONSURGE_TEST_RCLONE'), 'Set ONIONSURGE_TEST_RCLONE for the local S3 integration')
class RcloneS3Tests(unittest.TestCase):
    def test_real_stdin_upload_preserves_bytes_and_stored_cache_headers(self):
        objects, uploads = {}, []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_):
                pass

            def respond(self, status, body=b'', headers=None, head=False):
                self.send_response(status)
                self.send_header('Content-Length', str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if body and not head:
                    self.wfile.write(body)

            def do_HEAD(self):
                item = objects.get(urlsplit(self.path).path)
                if item is None:
                    return self.respond(404, head=True)
                self.respond(200, item[0], item[1], head=True)

            def do_GET(self):
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query)
                if 'list-type' in query:
                    prefix = query.get('prefix', [''])[0]
                    delimiter = query.get('delimiter', [''])[0]
                    contents, directories = [], set()
                    for path, (body, headers) in objects.items():
                        key = path.removeprefix('/fixture-bucket/')
                        if not key.startswith(prefix):
                            continue
                        suffix = key[len(prefix):]
                        if delimiter and delimiter in suffix:
                            directories.add(prefix + suffix.split(delimiter)[0] + delimiter)
                            continue
                        contents.append('<Contents><Key>' + escape(key) + '</Key><LastModified>2026-11-18T00:00:00.000Z</LastModified><ETag>' + headers['ETag'] + '</ETag><Size>' + str(len(body)) + '</Size><StorageClass>STANDARD</StorageClass></Contents>')
                    listing = '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>fixture-bucket</Name><Prefix>' + escape(prefix) + '</Prefix><KeyCount>' + str(len(contents) + len(directories)) + '</KeyCount><MaxKeys>1000</MaxKeys><IsTruncated>false</IsTruncated>'
                    listing += ''.join(contents) + ''.join('<CommonPrefixes><Prefix>' + escape(key) + '</Prefix></CommonPrefixes>' for key in sorted(directories))
                    return self.respond(200, (listing + '</ListBucketResult>').encode(), {'Content-Type': 'application/xml'})
                item = objects.get(parsed.path)
                if item is None:
                    return self.respond(404, b'<Error><Code>NoSuchKey</Code></Error>')
                self.respond(200, item[0], item[1])

            def do_PUT(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                headers = {'ETag': '"' + hashlib.md5(body).hexdigest() + '"',
                           'Last-Modified': 'Wed, 18 Nov 2026 00:00:00 GMT'}
                for key, value in self.headers.items():
                    if key.lower() in {'cache-control', 'content-type'} or key.lower().startswith('x-amz-meta-'):
                        headers[key] = value
                objects[urlsplit(self.path).path] = body, headers
                uploads.append(dict(self.headers))
                self.respond(200, headers={'ETag': headers['ETag']})

        with tempfile.TemporaryDirectory() as folder:
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                provider = pub.RcloneProvider('do', os.environ['ONIONSURGE_TEST_RCLONE'], 'fixture-bucket', 'onionsurge',
                    'http://127.0.0.1:' + str(server.server_port), 'nyc3', 'fixture-key', 'fixture-secret-only')
                # No real profiles, credentials, proxy settings or external endpoint.
                provider.env.update(HOME=folder, TMPDIR=folder, RCLONE_CONFIG_ONIONSURGE_ENV_AUTH='false',
                                    RCLONE_CONFIG_ONIONSURGE_FORCE_PATH_STYLE='true')
                for key, raw, immutable in [
                    ('manifest.json', b'{"fixture":"root"}\n', False),
                    ('releases/fixture/data.json', b'{"fixture":"' + b'x' * (pub.MAX_OBJECT - 15) + b'"}\n', True),
                ]:
                    self.assertIsNone(provider.get(key, pub.MAX_OBJECT, missing_ok=True))
                    provider.put(key, raw, immutable)
                    self.assertEqual(provider.get(key, pub.MAX_OBJECT), raw)
                    provider.verify_cache_control(key, immutable)
                    stored, headers = objects['/fixture-bucket/onionsurge/' + key]
                    self.assertEqual(pub.sha256(stored), pub.sha256(raw))
                    self.assertEqual(headers['Cache-Control'], pub.IMMUTABLE_CACHE_CONTROL if immutable else 'no-store')
                    self.assertEqual(headers['Content-Type'], 'application/json; charset=utf-8')
                    self.assertFalse({'x-amz-meta-uid', 'x-amz-meta-gid', 'x-amz-meta-mode'} & {h.lower() for h in headers})
                self.assertEqual(len(uploads), 2)
                self.assertTrue(all('fixture-key/' in row['Authorization'] for row in uploads))
                raw, headers = objects['/fixture-bucket/onionsurge/manifest.json']
                headers.pop('Cache-Control')
                self.assertEqual(provider.get('manifest.json', pub.MAX_ROOT), raw)
                with self.assertRaisesRegex(pub.PublicationError, 'cache_control_mismatch'):
                    provider.verify_cache_control('manifest.json', immutable=False)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
