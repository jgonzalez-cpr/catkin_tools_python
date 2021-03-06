# Copyright 2017 Clearpath Robotics Inc.
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

import os
import pkginfo
import re
import shutil
import subprocess
import sys

from catkin_tools.jobs.cmake import copy_install_manifest
from catkin_tools.jobs.cmake import generate_env_file
from catkin_tools.jobs.cmake import generate_setup_file
from catkin_tools.jobs.cmake import get_python_install_dir

from catkin_tools.jobs.utils import copyfiles
from catkin_tools.jobs.utils import loadenv
from catkin_tools.jobs.utils import makedirs
from catkin_tools.jobs.utils import rmfiles
from catkin_tools.utils import which

from catkin_tools.execution.jobs import Job
from catkin_tools.execution.stages import CommandStage
from catkin_tools.execution.stages import FunctionStage


PYTHON_EXEC = os.environ.get('PYTHON', sys.executable)
RSYNC_EXEC = which('rsync')


def renamepath(logger, event_queue, source_path, dest_path):
    """ FunctionStage functor that renames a file or directory, overwriting the
        destination if present. """
    if os.path.exists(dest_path):
        shutil.rmtree(dest_path)
    os.renames(source_path, dest_path)
    return 0


def fix_shebangs(logger, event_queue, pkg_dir, python_exec):
    """Process all files and change the shebangs if they are set to the global python
       to now use the python exec that we explicitly asked for.  This will ensure that 
       if you are building for python3 and source is using python that it will not use 
       the default version on the machine
    """
    logger.out("Changing shebangs for default python")
    for root, dirnames, file_list in os.walk(pkg_dir):
      for filename in file_list:
        modified = False
        if filename.endswith(('.py')):
            logger.out("Processing file ", filename)
            filepath = os.path.join(root, filename)
            with open(filepath, 'rb') as f:
                contents = f.read()

            new_shebang = ('#!%s' % python_exec).encode()
            # ensure we are using the correct python
            if re.match(b"#!/usr/bin/python(\s|$)", contents):
                logger.out("Modifying shebang from global python to python exec")
                contents = contents.replace(b'#!/usr/bin/python', new_shebang, 1)
                modified = True
            elif re.match(b"#!/usr/bin/env python(\s|$)", contents):
                logger.out("Modifying shebang from using env python to python exec")
                contents = contents.replace(b'#!/usr/bin/env python', new_shebang, 1)
                modified = True

            if modified:
                logger.out("Writing changes  to %s" % filename)
                with open(filepath, 'wb') as f:
                    f.write(contents)

    return 0

# catkin defaults the env to the pythonpath/version that is being used
# this was fine for 2.7 but since we are forcing everything to python3
# we need to fix the PYTHONPATH that is set by catkin in setup.sh
# It doesn't look like there is anyway to override how catkin is setting PYTHONPATH here
def fix_python3_install_space(logger, event_queue, install_space, old_python, new_python):
    """Modify the setup.sh in the python installs to have the correct PYTHONPATH
       :param: install_space: packages install space where the setup.sh script will be
    """
    old_python_path = ("/lib/python%s" % old_python).encode()
    new_python_path = ("/lib/python%s" % new_python).encode()
    filepath = os.path.join(install_space, "setup.sh")
    if os.path.exists(filepath):
        with open(filepath, 'rb') as f:
            contents = f.read()
            contents = contents.replace(old_python_path, new_python_path)

        if contents:
            logger.out("Modifying python path from %s to %s in %s" % (old_python_path, new_python_path, filepath))
            with open(filepath, 'wb') as f:
                f.write(contents)

    return 0

def determine_python_exec(cmake_args):
    """Parse the cmake args to determine if PYTHON_EXECUTATBLE was set.
       If it was not set it will default to the python that is executing catkin
    """
    global PYTHON_EXEC
    python_directive = [ x for x in cmake_args if 'PYTHON_EXECUTABLE' in x ]
    if python_directive:
        # remove the substring
        PYTHON_EXEC = python_directive[0].replace('-DPYTHON_EXECUTABLE=', "")

def determine_python_version():
    """Determine the major and minor python version information from the python
       that we are building for.
    """
    py_version_cmd = 'import sys; print("%s %s" % sys.version_info[0:2])'
    check_version = subprocess.check_output([PYTHON_EXEC, '-c', py_version_cmd]).split()
    python_version = { 'major' : int(check_version[0]),
                       'minor' : int(check_version[1])
                     }
    return python_version


def create_python_build_job(context, package, package_path, dependencies, force_cmake, pre_clean):

    # Package source space path
    pkg_dir = os.path.join(context.source_space_abs, package_path)

    # Package build space path
    build_space = context.package_build_space(package)

    # Package metadata path
    metadata_path = context.package_metadata_path(package)

    # Environment dictionary for the job, which will be built
    # up by the executions in the loadenv stage.
    job_env = dict(os.environ)

    # Some Python packages (in particular matplotlib) seem to struggle with
    # being built by ccache, so strip that out if present.
    def strip_ccache(cc_str):
        parts = cc_str.split()
        return ' '.join([part for part in parts if not 'ccache' in part])
    if 'CC' in job_env:
        job_env['CC'] = strip_ccache(job_env['CC'])
    if 'CXX' in job_env:
        job_env['CXX'] = strip_ccache(job_env['CXX'])

    # Get actual staging path
    dest_path = context.package_dest_path(package)
    final_path = context.package_final_path(package)


    # determine if python executable has been passed in
    determine_python_exec(context.cmake_args)

    # determine python version being used
    python_version = determine_python_version()

    # Create job stages
    stages = []

    # Load environment for job.
    stages.append(FunctionStage(
        'loadenv',
        loadenv,
        locked_resource='installspace',
        job_env=job_env,
        package=package,
        context=context
    ))

    # Create package metadata dir
    stages.append(FunctionStage(
        'mkdir',
        makedirs,
        path=metadata_path
    ))

    # Copy source manifest
    stages.append(FunctionStage(
        'cache-manifest',
        copyfiles,
        source_paths=[os.path.join(context.source_space_abs, package_path, 'package.xml')],
        dest_path=os.path.join(metadata_path, 'package.xml')
    ))

    # Check if this package supports --single-version-externally-managed flag, as some old
    # distutils packages don't, notably pyyaml. The following check is fast and cheap. A more
    # comprehensive check would be to parse the results of python setup.py --help or similar,
    # but that is expensive to do, since it has to occur at the start of the build.
    with open(os.path.join(pkg_dir, 'setup.py')) as f:
        setup_file_contents = f.read()
    svem_supported = re.search('(from|import) setuptools', setup_file_contents)

    # Python setup install
    stages.append(CommandStage(
        'python',
        [PYTHON_EXEC, 'setup.py',
         'build', '--build-base', build_space,
         'install',
         '--root', build_space,
         '--prefix', 'install'] +
        (['--single-version-externally-managed'] if svem_supported else []),
        cwd=pkg_dir
    ))

    # Special path rename required only on Debian.
    python_install_dir = get_python_install_dir()
    if 'dist-packages' in python_install_dir:
        python_install_dir_site = python_install_dir.replace('dist-packages', 'site-packages')
        if python_version['major'] == 3:
            python_install_dir = python_install_dir.replace('python%s.%s' % (python_version['major'], python_version['minor']), 'python%s' % python_version['major'])

        stages.append(FunctionStage(
            'debian-fix',
            renamepath,
            source_path=os.path.join(build_space, 'install', python_install_dir_site),
            dest_path=os.path.join(build_space, 'install', python_install_dir)
        ))

    # Create package install space.
    stages.append(FunctionStage(
        'mkdir-install',
        makedirs,
        path=dest_path
    ))


    # Copy files from staging area into final install path, using rsync. Despite
    # having to spawn a process, this is much faster than copying one by one
    # with native Python.
    stages.append(CommandStage(
        'install',
        [RSYNC_EXEC, '-a',
            os.path.join(build_space, 'install', ''),
            dest_path],
        cwd=pkg_dir,
        locked_resource='installspace'))

    # fix shebangs that point to the global space to use the python exec
    stages.append(FunctionStage(
        'fix-shebang',
        fix_shebangs,
        pkg_dir=dest_path,
        python_exec=PYTHON_EXEC,
        locked_resource=None if context.isolate_install else 'installspace'))

    # Determine the location where the setup.sh file should be created
    stages.append(FunctionStage(
        'setupgen',
        generate_setup_file,
        context=context,
        install_target=dest_path
    ))

    stages.append(FunctionStage(
        'envgen',
        generate_env_file,
        context=context,
        install_target=dest_path
    ))

    # fix the setup.sh which exports PYTHONPATH incorrectly for how we install python3 vs python3.5
    if python_version['major'] == 3:
        stages.append(FunctionStage(
            'fix_python3_install_space',
            fix_python3_install_space,
            install_space=dest_path,
            old_python="%s.%s" % (python_version['major'], python_version['minor']),
            new_python=python_version['major'],
            locked_resource='installspace'
        ))

    return Job(
        jid=package.name,
        deps=dependencies,
        env=job_env,
        stages=stages)


def create_python_clean_job(context, package, package_path, dependencies, dry_run,
                            clean_build, clean_devel, clean_install):
    stages = []

    # Package build space path
    build_space = context.package_build_space(package)

    # Package metadata path
    metadata_path = context.package_metadata_path(package)

    # Environment dictionary for the job, empty for a clean job
    job_env = {}

    stages = []

    return Job(
        jid=package.name,
        deps=dependencies,
        env=job_env,
        stages=stages)
