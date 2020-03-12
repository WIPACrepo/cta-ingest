#!/usr/bin/env python3
import argparse
import boto3
from boto3.s3.transfer import TransferConfig
import botocore
from functools import partial
import json
import logging
import re
import signal
from subprocess import PIPE, Popen
import sys
import threading
import os
from pprint import pprint
from pathlib import Path
from time import time

def _rmdirr(path):
    for fp in path.iterdir():
        fp.unlink()
    path.rmdir()

def _run_pipeline(cmd1, cmd2):
    p1 = Popen(cmd1, stdout=PIPE)
    p2 = Popen(cmd2, stdin=p1.stdout)
    p1.stdout.close() # Allow p1 to receive SIGPIPE if p2 exits.
    p2.communicate()
    if p2.returncode != 0:
        raise Exception('NonZeroReturnCode', p2.returncode, cmd1, cmd2)

class NoSuchKeyError(Exception):
    pass

class S3_Wrapper:
    def __init__(self, endpoint_url, bucket):
        s3_pool_size = 150
        boto_config = botocore.config.Config(max_pool_connections=s3_pool_size)
        self._s3r = boto3.resource('s3', endpoint_url=endpoint_url, config=boto_config)
        self._s3c = self._s3r.meta.client
        self._bucket = bucket
        self._s3b = self._s3r.Bucket(self._bucket)
        self._s3b.create()
        self._tx_config = TransferConfig(max_concurrency=s3_pool_size,
                                        multipart_threshold=2**20, 
                                        multipart_chunksize=2**20)
        self._progress_interval=120

    def get_from_json(self, key, **kwargs):
        obj = self._s3r.Object(self._bucket, key)
        try:
            body = obj.get()['Body'].read()
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] == 'NoSuchKey':
                if 'default' in kwargs:
                    return kwargs['default']
            raise NoSuchKeyError
        return json.loads(body)

    def put_as_json(self, state, key):
        body = json.dumps(state)
        self._s3c.put_object(Bucket=self._bucket, Key=key, Body=body)

    def download_file(self, key, path):
        self._s3c.download_file(self._bucket, key, path)

    def upload_file(self, path, key):
        label = '...' + path[-17:]
        size = Path(path).stat().st_size
        self._s3c.upload_file(path, self._bucket, key, Config=self._tx_config,
                 Callback=ProgressMeter(label, size, self._progress_interval))

    def delete_object(self, key):
        self._s3c.delete_object(Bucket=self._bucket, Key=key)

    def list_keys(self, prefix=''):
        return [obj.key for obj in self._s3b.objects.filter(Prefix=prefix)]

class ProgressMeter(object):
    # To simplify, assume this is hooked up to a single operation
    def __init__(self, label, size, update_interval=10):
        self._label = label
        self._size = size
        self._count = 0
        self._first_time = None
        self._first_count = None
        self._update_interval = update_interval
        self._last_update_time = None
        self._last_update_count = None
        self._lock = threading.Lock()

    def __readable_size(self, size):
        if size < 10**3:
            return '%s B' % int(size)
        elif size < 10**6:
            return '%.2f KiB' % (size/10**3)
        elif size < 10**9:
            return '%.2f MiB' % (size/10**6)
        elif size < 10**12:
            return '%.2f GiB' % (size/10**9)
        else:
            return '%.2f TiB' % (size/10**12)

    def __readable_time(self, time):
        time = int(round(time))
        seconds = time % 60
        minutes = (time // 60) % 60
        hours = time // (60 * 60)
        if hours:
            return f'{hours}h {minutes}m {seconds}s'
        elif minutes:
            return f'{minutes}m {seconds}s'
        else:
            return f'{seconds}s'

    def __call__(self, num_bytes):
        with self._lock:
            now = time()
            if self._first_time is None:
                self._first_time = now
                self._first_count = num_bytes
                # trick to display initial stats earlier than self._update_interval
                self._last_update_time = now - self._update_interval + 10
                self._last_update_count = num_bytes
            self._count += num_bytes
            t_observed = now - self._first_time
            t_since_update = now - self._last_update_time
            b_since_update = self._count - self._last_update_count

            rs = partial(self.__readable_size)
            rt = partial(self.__readable_time)
            if self._count == self._size:
                sys.stdout.write(f'{self._label} {rs(self._size)} in ~{rt(t_observed)}\n')
                sys.stdout.flush()
                return
            if t_since_update >= self._update_interval:
                percent = (self._count / self._size) * 100
                update_delta = self._count - self._last_update_count 
                update_rate = update_delta / t_since_update # XXX why is this negative sometimes?
                average_rate = self._count / t_observed
                t_remaining_cur = (self._size - self._count) / update_rate
                sys.stdout.write(
                        f'{self._label: <20} {rt(t_observed): <7}  '
                        f'{rs(self._count): >10} / {rs(self._size)} {percent: 3.0f}%  '
                        f'{rs(update_rate)}/s {rs(average_rate)}/s  '
                        f'ETA: {rt(t_remaining_cur)}\n')
                sys.stdout.flush()
                self._last_update_time = now
                self._last_update_count = self._count

def disassemble(s3w, work_dir, part_size, dry_run):
    my_state_key = 'disassemble.json'
    my_state = s3w.get_from_json(my_state_key, default={})
    origin = s3w.get_from_json('origin.json', default={})
    target = s3w.get_from_json('target.json')

    my_delivered = set(my_state).intersection(target)
    my_unprocessed = set(origin) - set(my_state) - set(target) - set(my_delivered)

    if dry_run:
        logging.info(f'Dry run: would have cleaned-up {my_delivered}')
        logging.info(f'Dry run: would have processed {my_unprocessed}')
        return

    for fname in my_delivered:
        logging.info(f'Cleaning up {Path(work_dir, fname)}')
        _rmdirr(Path(work_dir, fname))
        my_state.pop(fname)
        s3w.put_as_json(my_state, my_state_key)

    for fname in my_unprocessed:
        logging.info(f'Compressing and splitting {fname}')
        chunk_dir = Path(work_dir, fname)
        if chunk_dir.exists():
            _rmdirr(chunk_dir)
        chunk_dir.mkdir(parents=True)
        # Compressing with --threads=0 seems to use 30-75% of CPU
        zstd_cmd = ['zstd', '--threads=0', '--stdout', origin[fname]['path']]
        split_cmd = ['split', '-b', str(part_size), '-', str(chunk_dir) + '/']
        _run_pipeline(['nice', '-n', '19'] + zstd_cmd, split_cmd)
        my_state[fname] = [str(f) for f in chunk_dir.iterdir()]
        s3w.put_as_json(my_state, my_state_key)

def download(s3w, work_dir):
    my_state_key = 'download.json'
    my_state = s3w.get_from_json(my_state_key, default={})
    src_state = s3w.get_from_json('upload.json', default={})
    target = s3w.get_from_json('target.json')

    my_delivered = set(my_state).intersection(target)
    for fname in my_delivered:
        logging.info(f'Cleaning up {Path(work_dir, fname)}')
        _rmdirr(Path(work_dir, fname))
        my_state.pop(fname)
        s3w.put_as_json(my_state, my_state_key)

    my_unprocessed = set(src_state) - set(my_state) - set(target)
    for origin_path in my_unprocessed:
        part_keys = src_state[origin_path]
        my_state.setdefault(origin_path, [])
        chunks_dir = work_dir / Path(origin_path)
        chunks_dir.mkdir(parents=True, exist_ok=True)
        for part_key in part_keys:
            logging.info(f'Downloading {origin_path} {part_key}')
            dst_path = str(chunks_dir / Path(part_key).name)
            s3w.download_file(part_key, dst_path)
            my_state[origin_path].append(dst_path)
        s3w.put_as_json(my_state, my_state_key)

def reassemble(s3w, work_dir, dst_dir):
    work_dir.mkdir(parents=True, exist_ok=True)
    dst_dir.mkdir(parents=True, exist_ok=True)
    src_state = s3w.get_from_json('download.json')
    origin_state = s3w.get_from_json('origin.json')

    for origin_path, part_paths in src_state.items():
        logging.info(f'Processing {origin_path} from {len(part_paths)} parts')
        output_path = Path(work_dir, Path(origin_path).name)
        cat_cmd = ['cat'] + sorted(part_paths)
        zstd_cmd = ['zstd', '--quiet', '--force', '--decompress', '-o', str(output_path)]
        _run_pipeline(cat_cmd, zstd_cmd)
        origin_file = origin_state[origin_path]
        os.utime(output_path, (origin_file['atime'], origin_file['mtime']))
        output_path.chmod(0o444)
        target_path = dst_dir/output_path.name
        if target_path.exists():
            logging.error(f'{target_path} exists')
            continue
        else:
            logging.info(f'{dst_dir/output_path.name} has been reassembled')
            output_path.rename(dst_dir/output_path.name)

def refresh_terminus(s3w, root_dir, fn_patterns, my_state_key):
    root_dir = root_dir.resolve()
    state = {}
    relevant_files = [fp for fp in root_dir.iterdir()
                if fp.is_file() and any([re.match(pat, fp.name) for pat in fn_patterns])]
    logging.info(f'Found {len(relevant_files)} files matching {fn_patterns} in {root_dir}')
    for fp in relevant_files:
        state[str(fp.relative_to(root_dir))] = {
                'path': str(fp.resolve()),
                'size':fp.stat().st_size,
                'mtime':fp.stat().st_mtime,
                'atime':fp.stat().st_atime,
                'ts':time(),}
    s3w.put_as_json(state, my_state_key)

def show_status(s3w):
    # XXX handle missing state exceptions
    target = s3w.get_from_json('target.json')
    origin = s3w.get_from_json('origin.json')

    undelivered = [fn for fn in origin if fn not in target]
    present = [fn for fn in origin if fn in target]
    mismatched = [fn for fn in present if origin[fn]['size'] != target[fn]['size']]

    print('Present:', len(present))
    print('Undelivered:', undelivered)
    print('Mismatched:', mismatched)

def upload(s3w, dry_run):
    my_state_key = 'upload.json'
    my_state = s3w.get_from_json(my_state_key, default={})
    src_state = s3w.get_from_json('disassemble.json', default={})
    target = s3w.get_from_json('target.json')

    my_delivered = set(my_state).intersection(target)
    my_unprocessed = set(src_state) - set(my_state) - set(target) - set(my_delivered)

    if dry_run:
        logging.info(f'Dry run: would have cleaned-up {my_delivered}')
        logging.info(f'Dry run: would have processed {my_unprocessed}')
        return

    for fname in my_delivered:
        for key in my_state[fname]:
            logging.info(f'Cleaning up {key}')
            s3w.delete_object(key)
        my_state.pop(fname)
        s3w.put_as_json(my_state, my_state_key)

    uploaded_parts = s3w.list_keys(prefix='parts')
    for fname in my_unprocessed:
        my_state.setdefault(fname, [])
        for part_path in src_state[fname]:
            key = 'parts' + part_path
            if key in uploaded_parts:
                logging.warn(f'Key {key} already uploaded')
                continue
            logging.info(f'Uploading {part_path} as {key}')
            s3w.upload_file(part_path, key)
            my_state[fname].append(key)
        s3w.put_as_json(my_state, my_state_key)

def main():
    def arg_formatter(max_help_position, width=90):
        return lambda prog: argparse.ArgumentDefaultsHelpFormatter(prog,
                                        max_help_position=max_help_position, width=width)
    def _abs_path(path):
        return Path(path).resolve()

    parser = argparse.ArgumentParser(
            description='Description XXX CTA Ingest',
            formatter_class=arg_formatter(27))
    parser.add_argument('-v', '--verbose', action='store_true', default=False,
            help='verbose logging')

    subpars = parser.add_subparsers(title='commands', dest='command',
            description='Use "%(prog)s <command> -h" or similar to get command help.')
    par_status = subpars.add_parser('status', formatter_class=argparse.ArgumentDefaultsHelpFormatter,
            help='Display status summary')
    par_refresh_origin = subpars.add_parser('refresh_origin', formatter_class=arg_formatter(27),
            help='XXX')
    par_refresh_origin.add_argument('-f', dest='filters', nargs='*', metavar='RE', default=['.*'],
            help='Filter file name by regular expression')
    par_refresh_origin.add_argument('path', metavar='PATH', type=_abs_path,
            help='Path to monitor')

    par_refresh_target = subpars.add_parser('refresh_target', formatter_class=arg_formatter(27),
            help='XXX')
    par_refresh_target.add_argument('path', metavar='PATH', type=_abs_path,
            help='Path to monitor')

    par_disassemble = subpars.add_parser('disassemble', formatter_class=argparse.ArgumentDefaultsHelpFormatter,
            help='disassemble')
    par_disassemble.add_argument('path', metavar='PATH', type=_abs_path,
            help='Destination path')
    par_disassemble.add_argument('--part-size-gb', metavar='GB', default=10.0, type=float,
            help='Part size in GB')
    par_disassemble.add_argument('--dry-run', default=False, action='store_true',
            help='dry run')
    
    par_upload = subpars.add_parser('upload', formatter_class=argparse.ArgumentDefaultsHelpFormatter,
            help='Upload')
    par_upload.add_argument('--dry-run', default=False, action='store_true',
            help='dry run')
    par_upload.add_argument('--timeout', metavar='SECONDS', type=int,
            help='terminate after this amount of time')

    par_download = subpars.add_parser('download', formatter_class=argparse.ArgumentDefaultsHelpFormatter,
            help='download')
    par_download.add_argument('path', metavar='PATH', type=_abs_path,
            help='Work dir')
    
    par_reassemble = subpars.add_parser('reassemble', formatter_class=argparse.ArgumentDefaultsHelpFormatter,
            help='reassemble')
    par_reassemble.add_argument('path', metavar='PATH', type=_abs_path,
            help='Work dir')
    par_reassemble.add_argument('dst_path', metavar='PATH', type=_abs_path,
            help='Dst dir')

    s3_grp = parser.add_argument_group('S3 options',
            description='Note that S3 credential arguments are optional. '
                'See the "Configuring Credentials" section of boto3 library documentation for details.')
    s3_grp.add_argument('-u', '--s3-url', metavar='URL', default='https://rgw.icecube.wisc.edu',
            help='S3 endpoint URL')
    s3_grp.add_argument('-b', '--bucket', metavar='NAME', required=True,
            help='S3 bucket name')
    s3_grp.add_argument('-a', dest='access_key_id',
            help='S3 access key id override')
    s3_grp.add_argument('-s', dest='secret_access_key',
            help='S3 secret access key override')

    args = parser.parse_args()

    log_level = (logging.INFO if args.verbose else logging.WARN)
    logging.basicConfig(level=log_level,
            format='%(levelname)s %(funcName)s() %(message)s')
    logging.info(f'Arguments: {args}')

    if args.command is None:
        parser.print_help()
        parser.exit()

    s3w = S3_Wrapper(args.s3_url, args.bucket)

    if args.command == 'status':
        show_status(s3w)
    elif args.command == 'refresh_origin':
        refresh_terminus(s3w, args.path, args.filters, 'origin.json')
    elif args.command == 'refresh_target':
        refresh_terminus(s3w, args.path, ['.*'], 'target.json')
    elif args.command == 'disassemble':
        disassemble(s3w, args.path, int(args.part_size_gb * 2**30), args.dry_run)
    elif args.command == 'upload':
        if args.timeout:
            signal.alarm(args.timeout)
        upload(s3w, args.dry_run)
    elif args.command == 'download':
        download(s3w, args.path)
    elif args.command == 'reassemble':
        reassemble(s3w, args.path, args.dst_path)

if __name__ == '__main__':
    sys.exit(main())

