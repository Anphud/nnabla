# Copyright (c) 2017 Sony Corporation. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import boto3
import csv
import os
import shutil
import tarfile
import tempfile

from nnabla.logger import logger


def upload_command(args):
    print(args)
    tmpdir = args.tmp
    if tmpdir is None:
        tmpdir = tempfile.mkdtemp()

    tarfiles = []
    if os.path.exists(args.source):
        with open(args.source, 'r') as f:
            files = []
            current_file = []
            current_file_size = 0

            data_files = {}
            num_of_data_file = 0
            rows = []

            csv_lines = f.readlines()
            csvreader = csv.reader(csv_lines)
            first_line = True
            for row in csvreader:
                if first_line:
                    first_line = False
                    rows.append(row)
                else:
                    new_row = []
                    for item in row:
                        data_file = None
                        if os.path.exists(item):
                            data_file = item
                        elif os.path.exists(os.path.join(os.path.dirname(args.source), item)):
                            data_file = os.path.join(
                                os.path.dirname(args.source), item)

                        if data_file is not None:
                            data_filesize = os.path.getsize(data_file)
                            name = os.path.join('file', '{:010d}_{}'.format(
                                num_of_data_file, os.path.basename(data_file)))
                            data_files[name] = [num_of_data_file, data_filesize, os.path.basename(
                                data_file), os.path.abspath(data_file)]
                            new_row.append(name)

                            # Split file
                            if (current_file_size + data_filesize) > 1000000000 * args.size:
                                logger.log(99, 'Split {}'.format(
                                    current_file_size))
                                files.append(current_file)
                                current_file = []
                                current_file_size = 0

                            current_file.append(name)
                            current_file_size += data_filesize

                            num_of_data_file += 1

                        else:
                            new_row.append(item)

                    rows.append(new_row)
            files.append(current_file)

            csvfilename = os.path.join(
                tmpdir, os.path.basename(args.source))
            with open(csvfilename, 'w') as f:
                for row in rows:
                    f.write(','.join(row) + '\n')
            for n, filelist in enumerate(files):
                tarfilename = os.path.join(
                    tmpdir, 'dataset_{:04d}.tar'.format(n))
                logger.log(99, 'Create {}'.format(tarfilename))

                with tarfile.open(tarfilename, 'w') as tar:
                    if n == 0:
                        tar.add(csvfilename, os.path.basename(csvfilename))
                    for fn in filelist:
                        tar.add(data_files[fn][3], fn)
                tarfiles.append(tarfilename)

    print(args.dest)
    if args.dest[0:5] == 's3://':
        print(args.dest[5:])
        bucketname, basekey = args.dest[5:].split('/', 1)
        print(bucketname, basekey)
        s3_bucket = boto3.session.Session().resource('s3').Bucket(bucketname)
        for tar in tarfiles:
            logger.log(99, 'Upload {} start'.format(tar))
            with open(tar, 'rb') as f:
                s3_bucket.put_object(
                    Key=basekey + '/' + os.path.basename(tar), Body=f)
                logger.log(99, 'Upload {} done.'.format(tar))

    if args.tmp is None:
        shutil.rmtree(tmpdir, ignore_errors=True)