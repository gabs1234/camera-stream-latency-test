import argparse
import getpass
import math
import os
import ssl
import sys
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('must be a finite positive number')
    return number


def main():
    parser = argparse.ArgumentParser(description='Direct MJPEG camera latency using scheduled timestamp QR codes')
    commands = parser.add_subparsers(dest='command', required=True)
    emit = commands.add_parser('emit', help='fullscreen timestamped QR source')
    emit.add_argument('--interval-ms', type=positive, default=200)
    emit.add_argument('--visible-ms', type=positive, default=150)
    emit.add_argument('--source-log', default='source-timing.csv')
    emit.add_argument('--clock-jump-ms', type=positive, default=20)
    measure = commands.add_parser('measure', help='receive HTTP(S) multipart MJPEG')
    measure.add_argument('--url', help='stream URL; defaults to CAMERA_URL from environment or .env')
    measure.add_argument('--output', default='measurements.csv')
    measure.add_argument('--username', help='HTTP username; defaults to CAMERA_USERNAME')
    measure.add_argument('--auth', choices=['digest', 'basic'], default='digest')
    measure.add_argument('--password-env', help='password variable name; defaults to CAMERA_PASSWORD, then prompt')
    measure.add_argument('--ca-file', help='PEM CA file for a private camera certificate')
    measure.add_argument('--timeout', type=positive, default=5, help='network inactivity timeout, seconds')
    measure.add_argument('--headless', action='store_true')
    measure.add_argument('--duration', type=positive, help='stop after this many seconds')
    measure.add_argument('--clock-jump-ms', type=positive, default=20)
    report = commands.add_parser('report', help='summarize and optionally join source timing into a new CSV')
    report.add_argument('--measurements', required=True)
    report.add_argument('--source-log')
    report.add_argument('--output', help='new joined CSV (never overwrite measurements)')
    args = parser.parse_args()
    if args.command == 'emit':
        if min(args.interval_ms, args.visible_ms) < 1:
            parser.error('interval and visibility must be at least 1 ms (100–200 ms is more practical)')
        if args.visible_ms >= args.interval_ms:
            parser.error('--visible-ms must be less than --interval-ms to leave a black interval')
        if args.visible_ms < 100 or args.interval_ms-args.visible_ms < 20:
            print('Warning: short visibility/black gap may miss frames or overlap exposures at 25–60 fps.', file=sys.stderr)
        from .emitter import emit as run
    elif args.command == 'measure':
        # Read only the working directory's .env, without changing the process
        # environment or expanding ${...} inside passwords. Exported values win.
        config = {**dotenv_values(Path.cwd() / '.env', interpolate=False), **os.environ}
        if args.url is None:
            args.url = config.get('CAMERA_URL')
        if args.username is None:
            args.username = config.get('CAMERA_USERNAME')
        if not args.url:
            parser.error('provide --url or set CAMERA_URL in the environment or .env')
        try:
            url = urlsplit(args.url)
            if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.fragment:
                raise ValueError()
        except ValueError:
            parser.error('use an HTTP(S) URL without embedded credentials or a fragment')
        if args.password_env and not args.username:
            parser.error('--password-env requires --username')
        args.password = None
        if args.username:
            if args.password_env:
                if config.get(args.password_env) is None:
                    parser.error('password environment variable is not set')
                args.password = config[args.password_env]
            elif config.get('CAMERA_PASSWORD'):
                args.password = config['CAMERA_PASSWORD']
            else:
                args.password = getpass.getpass('Camera password: ')
        args.tls_context = ssl.create_default_context(cafile=args.ca_file)
        from .receiver import measure as run
    else:
        from .report import report as run
    try:
        run(args)
    except (OSError, ValueError, RuntimeError) as exc:
        # Avoid exception text that might expose URLs, query parameters or passwords.
        print(f'Error: {type(exc).__name__}. Check file paths (outputs must be new), inputs and display availability.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
