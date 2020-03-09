#!/usr/bin/env python
import argparse
import boto3
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError
from functools import partial
import json
import logging
import re
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
        raise Exception('NonZeroReturnCode')

class NoSuchKeyError(Exception):
    pass

class S3_Wrapper:
    def __init__(self, endpoint_url, bucket, concurrency=80, chunksize=2**20, progress_interval=0.1):
        self._s3r = boto3.resource('s3', endpoint_url=endpoint_url)
        self._s3c = self._s3r.meta.client
        self._bucket = bucket
        self._s3b = self._s3r.Bucket(self._bucket)
        self._s3b.create()
        self._tx_config = TransferConfig(max_concurrency=concurrency,
                                        multipart_threshold=chunksize, 
                                        multipart_chunksize=chunksize)
        self._progress_interval=progress_interval

    def get_from_json(self, key, **kwargs):
        obj = self._s3r.Object(self._bucket, key)
        try:
            body = obj.get()['Body'].read()
        except ClientError as e:
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
                self._last_update_time = now
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
                t_remaining_avg = (self._size - self._count) / average_rate
                t_remaining_cur = (self._size - self._count) / update_rate
                sys.stdout.write(
                        f'{self._label: <20} {rt(t_observed): <11}  '
                        f'{rs(self._count): >10} / {rs(self._size)} {percent: 3.0f}%  '
                        f'{rs(update_rate)}/s {rs(average_rate)}/s  '
                        f'ETA: {rt(t_remaining_cur)} {rt(t_remaining_avg)}\n')
                sys.stdout.flush()
                self._last_update_time = now
                self._last_update_count = self._count

def disassemble(work_dir, s3w):
    my_state_key = 'disassemble.json'
    my_state = s3w.get_from_json(my_state_key, default={})
    origin = s3w.get_from_json('origin.json', default={})
    target = s3w.get_from_json('target.json', default={})
    
    my_delivered = set(my_state).intersection(target)
    for fname in my_delivered:
        _rmdirr(Path(work_dir, fname))
        my_state.pop(fname)
        s3w.put_as_json(my_state, my_state_key)

    my_unprocessed = set(origin) - set(my_state) - set(target)
    for fname in my_unprocessed:
        chunk_dir = Path(work_dir, fname)
        if chunk_dir.exists():
            _rmdirr(chunk_dir)
        chunk_dir.mkdir(parents=True)
        zstd_cmd = ['nice', '-n', '19', 'zstd', '--stdout', origin[fname]['path']]
        split_cmd = ['split', '-b', str(10*10**6), '-', str(chunk_dir) + '/']
        _run_pipeline(zstd_cmd, split_cmd)
        my_state[fname] = [str(f) for f in chunk_dir.iterdir()]
        s3w.put_as_json(my_state, my_state_key)

def download(work_dir, s3w):
    my_state_key = 'download.json'
    my_state = s3w.get_from_json(my_state_key, default={})
    src_state = s3w.get_from_json('upload.json', default={})
    target = s3w.get_from_json('target.json', default={})

    my_delivered = set(my_state).intersection(target)
    for fname in my_delivered:
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
            dst_path = str(chunks_dir / Path(part_key).name)
            s3w.download_file(part_key, dst_path)
            my_state[origin_path].append(dst_path)
        s3w.put_as_json(my_state, my_state_key)

def reassemble(work_dir, dst_dir, s3w):
    work_dir.mkdir(parents=True, exist_ok=True)
    dst_dir.mkdir(parents=True, exist_ok=True)
    src_state = s3w.get_from_json('download.json')
    origin_state = s3w.get_from_json('origin.json')

    for origin_path, part_paths in src_state.items():
        output_path = Path(work_dir, Path(origin_path).name)
        cat_cmd = ['cat'] + part_paths
        zstd_cmd = ['zstd', '--quiet', '--force', '--decompress', '-o', str(output_path)]
        _run_pipeline(cat_cmd, zstd_cmd)
        origin_file = origin_state[origin_path]
        os.utime(output_path, (origin_file['atime'], origin_file['mtime']))
        output_path.chmod(0o444)
        target_path = dst_dir/output_path.name
        if target_path.exists():
            continue
        else:
            output_path.rename(dst_dir/output_path.name)

def refresh_terminus(root_dir, fn_patterns, my_state_key, s3w):
    root_dir = root_dir.resolve()
    state = {}
    relevant_files = [fp for fp in root_dir.iterdir()
                if fp.is_file() and any([re.match(pat, fp.name) for pat in fn_patterns])]
    for fp in relevant_files:
        state[str(fp.relative_to(root_dir))] = {
                'path': str(fp.resolve()),
                'size':fp.stat().st_size,
                'mtime':fp.stat().st_mtime,
                'atime':fp.stat().st_atime,}
    s3w.put_as_json(state, my_state_key)

def show_status(s3w):
    target = s3w.get_from_json('target.json', default={})
    origin = s3w.get_from_json('origin.json', default={})

    undelivered = [fn for fn in origin if fn not in target]
    present = [fn for fn in origin if fn in target]
    mismatched = [fn for fn in present
                        if origin[fn]['size'] != target[fn]['size']]

    print('Present:', len(present))
    print('Undelivered:', undelivered)
    print('Mismatched:', mismatched)

def upload(s3w):
    my_state_key = 'upload.json'
    my_state = s3w.get_from_json(my_state_key, default={})
    src_state = s3w.get_from_json('disassemble.json', default={})
    target = s3w.get_from_json('target.json', default={})

    my_delivered = set(my_state).intersection(target)
    for fname in my_delivered:
        for key in my_state[fname]:
            s3w.delete_object(key)
        my_state.pop(fname)
        s3w.put_as_json(my_state, my_state_key)

    uploaded_parts = s3w.list_keys(prefix='parts')
    my_unprocessed = set(src_state) - set(target)
    for fname in my_unprocessed:
        my_state.setdefault(fname, [])
        for part_path in src_state[fname]:
            key = 'parts' + part_path
            if key in uploaded_parts:
                continue
            s3w.upload_file(part_path, key)
            my_state[fname].append(key)
        s3w.put_as_json(my_state, my_state_key)

def main():
    def arg_formatter(max_help_position, width=90):
        return lambda prog: argparse.ArgumentDefaultsHelpFormatter(prog,
                                        max_help_position=max_help_position, width=width)
    def _abs_path(path):
        return Path(path).resolve()
    parser = argparse.ArgumentParser(prog='cta-ingest',
            description='Description XXX CTA Ingest',
            formatter_class=arg_formatter(27))
    parser.add_argument('-v', '--verbose', action='store_true', default=False,
            help='verbose logging')

    subpars = parser.add_subparsers(title='optional commands', dest='command',
            description='The default command is "status". '
                'Use "%(prog)s <command> -h" or similar to get command help.')
    par_status = subpars.add_parser('status', help='Display status summary')
    par_adv = subpars.add_parser('adv', help='Run an advanced subcommand')
    subpar_adv = par_adv.add_subparsers(title='advanced subcommands', dest='adv_command')

    par_adv_refresh_origin = subpar_adv.add_parser('refresh_origin', help='XXX',
                                    formatter_class=arg_formatter(27))
    par_adv_refresh_origin.add_argument('-f', dest='filters', nargs='*', metavar='RE', default=['.*'],
            help='Filter file name by regular expression')
    par_adv_refresh_origin.add_argument('path', metavar='PATH', type=_abs_path,
            help='Path to monitor')

    par_adv_refresh_target = subpar_adv.add_parser('refresh_target', help='XXX',
                                    formatter_class=arg_formatter(27))
    par_adv_refresh_target.add_argument('path', metavar='PATH', type=_abs_path,
            help='Path to monitor')

    par_adv_disassemble = subpar_adv.add_parser('disassemble', help='disassemble')
    par_adv_disassemble.add_argument('path', metavar='PATH', type=_abs_path,
            help='Destination path')
    
    par_adv_upload = subpar_adv.add_parser('upload', help='Upload')

    par_adv_download = subpar_adv.add_parser('download', help='download')
    par_adv_download.add_argument('path', metavar='PATH', type=_abs_path,
            help='Work dir')
    
    par_adv_reassemble = subpar_adv.add_parser('reassemble', help='reassemble')
    par_adv_reassemble.add_argument('path', metavar='PATH', type=_abs_path,
            help='Work dir')
    par_adv_reassemble.add_argument('dst_path', metavar='PATH', type=_abs_path,
            help='Dst dir')

    s3_grp = parser.add_argument_group('S3 protocol options',
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

    if args.command is None:
        parser.print_help()
        parser.exit()
    
    log_level = (logging.INFO if args.verbose else logging.WARN)
    logging.basicConfig(level=log_level, format='%(asctime)-23s %(levelname)s %(message)s')

    s3w = S3_Wrapper(args.s3_url, args.bucket)

    if args.command == 'status':
        show_status(s3w)
    elif args.command == 'adv':
        if args.adv_command is None:
            parser.exit('Error: Command "adv" requires a subcommand')
        elif args.adv_command == 'refresh_origin':
            refresh_terminus(args.path, args.filters, 'origin.json', s3w)
        elif args.adv_command == 'refresh_target':
            refresh_terminus(args.path, ['.*'], 'target.json', s3w)
        elif args.adv_command == 'disassemble':
            disassemble(args.path, s3w)
        elif args.adv_command == 'upload':
            upload(s3w)
        elif args.adv_command == 'download':
            download(args.path, s3w)
        elif args.adv_command == 'reassemble':
            reassemble(args.path, args.dst_path, s3w)

if __name__ == '__main__':
    sys.exit(main())
