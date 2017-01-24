# Copyright (C) 2017 Marco Barisione
#
# Released under the terms of the GNU LGPL license version 2.1 or later.

import imp
import inspect
import os
import re
import shutil
import textwrap
import traceback

import dirs
import pathutils

from log import verbose


class DefinitionError(Exception):
    '''
    An error due to the user image definition containing mistakes.
    '''

    def __init__(self, definition_file_path, msg):
        '''
        Initializes a DefinitionError instance.

        definition_file_path - The path of the definition file which caused the error.
        msg - The exception error message.
        '''
        super(DefinitionError, self).__init__(msg)

        self._definition_file_path = definition_file_path

    @property
    def definition_file_path(self):
        '''
        The path of the definition file which caused the error to happen.
        '''
        return self._definition_file_path


_g_props_all_properties = {}

def props_property(fget, *args, **kwargs):
    _g_props_all_properties[fget.func_name] = fget
    return property(fget, *args, **kwargs)


class DefinitionProperties(object):
    '''
    Instructions on how to generate the image and what to include in it.
    '''

    _eval_regex = re.compile(r'\$\(([a-zA-Z0-9._-]*)\)')

    def __init__(self, image_name, definition_file_path, host_system):
        '''
        Initializes a DefinitionProperties instance.
        '''
        self._image_name = image_name
        self._definition_file_path = definition_file_path
        self._host_system = host_system

        self._username = host_system.username
        self._user_home = host_system.user_home
        self._distro = 'ubuntu'
        self._packages = []
        self._additional_archs = []

        self.maintainer = None

        self._eval_map = {
            'host.username': lambda: self._host_system.username,
            'host.userhome': lambda: self._host_system.user_home,
            'host.hostname': lambda: self._host_system.hostname,
            }

    def __str__(self):
        res = [
            'DefinitionProperties(image_name=%s, definition_file_path=%s, host_system=%s)' %
            (
                repr(self._image_name),
                repr(self._definition_file_path),
                repr(self._host_system),
            )
            ]
        for prop_name, getter in _g_props_all_properties.iteritems():
            res.append('    %s = %s' % (prop_name, repr(getter(self))))
        return '\n'.join(res)

    def eval(self, in_string):
        '''
        Replaces variables in in_string and returns the new string.

        FIXME: Document the variable syntanx and valid variables.
        '''
        def eval_cb(match):
            var_name = match.group(1)
            replacement_cb = self._eval_map.get(var_name)
            if replacement_cb is None:
                raise DefinitionError(
                    self._definition_file_path,
                    'The variable "$(%s)" is not valid (in string "%s").' %
                    (var_name, match.string))
            return replacement_cb()

        return self._eval_regex.sub(eval_cb, in_string)

    @props_property
    def image_name(self):
        return self._image_name

    @props_property
    def username(self):
        return self._username

    @username.setter
    def username(self, username):
        # FIXME: check for the validity of the new username
        self._username = username

    @props_property
    def user_home(self):
        return self._user_home

    @user_home.setter
    def user_home(self, user_home):
        '''
        Sets the user home directory for the user in the container.

        It's recommended to keep the home directory identical in the host and
        container. This helps in case any tool write a container path into a
        file that is later accessed from the host.
        '''
        self._user_home = user_home

    @props_property
    def packages(self):
        return self._packages

    @props_property
    def distro(self):
        return self._distro

    @distro.setter
    def distro(self, distro):
        # FIXME: handle tags.
        if distro not in ('ubuntu', 'debian'):
            raise DefinitionError(self._definition_file_path,
                                  'Invalid distibution: "%s".' % distro)

        self._distro = distro

    @props_property
    def additional_archs(self):
        return self._additional_archs


class Builder(object):
    '''
    Builds a Dockerfile and related files.
    '''

    def __init__(self, image_name, src_dir, dst_dir, host_system):
        '''
        Initializes a Builds instance.

        image_name - the name of the image.
        src_dir - the directory where the definition file and other files are.
        dst_dir - the directory where to put the Dockerfile and related files.
        '''
        self._image_name = image_name
        self._src_dir = src_dir
        self._dst_dir = dst_dir
        self._host_system = host_system

        self._copyable_files_dir = os.path.join(self._dst_dir, 'files')
        pathutils.makedirs(self._copyable_files_dir)

    def generate(self):
        '''
        Prepare the directory with the Dockerfile and all the other required
        files.

        If something goes wrong due to the user configuration or definition
        file, then a DefinitionError is raised.
        '''

        # Load the definition file.
        definition_path = os.path.join(self._src_dir, 'definition.py')

        try:
            with open(definition_path) as definition_file:

                try:
                    definition = imp.load_module(
                        'definition',
                        definition_file,
                        definition_path,
                        ('.py', 'r', imp.PY_SOURCE))
                except Exception as exc:
                    raise DefinitionError(
                        definition_path,
                        'The definition file "%s" couldn\'t be loaded because it contains an '
                        'error: %s.\n\n%s' % (definition_path, exc, traceback.format_exc()))

        except IOError as exc:
            raise DefinitionError(
                definition_path,
                'The definition file "%s" couldn\'t be opened: %s.\n\n%s' %
                (definition_path, exc, traceback.format_exc()))

        # Get the setup_image function.
        setup_image = getattr(definition, 'setup_image', None)
        if setup_image is None:
            raise DefinitionError(
                definition_path,
                'The definition file "%s" doesn\'t contain a "setup_image" method (which should '
                'accept an argument of type "DefinitionProperties").' % definition_path)

        try:
            args_spec = inspect.getargspec(setup_image)
        except TypeError as exc:
            raise DefinitionError(
                definition_path,
                'The definition file "%s" does contain a "setup_image" attribute, but it should be '
                'a method accepting an argument of type "DefinitionProperties".' % definition_path)

        if len(args_spec.args) != 1:
            raise DefinitionError(
                definition_path,
                'The definition file "%s" does contain a "setup_image" method, but it should '
                'accept a single argument of type "DefinitionProperties".' % definition_path)

        # Execute it.
        props = DefinitionProperties(self._image_name, definition_path, self._host_system)

        try:
            setup_image(props)
        except BaseException as exc:
            raise DefinitionError(
                definition_path,
                'An exception was raised while executing "setup_image" from the definition '
                'file "%s": %s.\n\n%s' %
                (definition_path, exc, traceback.format_exc()))

        # And finally generate the docker-related files from the DefinitionProperties.
        self._generate_docker_stuff(props)

    def _make_file_copyable(self, host_file_path):
        '''
        Allow the file at path host_file_path (on the host) to be copied to the image by
        creating a hard link to it from the directory sent to the Docker daemon.

        return value - the path of the hard link, relative to the destination directory.
        '''
        # FIXME: what if two files have the same basenames?
        link_path = os.path.join(self._copyable_files_dir, os.path.basename(host_file_path))
        os.link(host_file_path, link_path)

        assert link_path.startswith(self._dst_dir)
        return os.path.relpath(link_path, start=self._dst_dir)

    def _generate_docker_stuff(self, props):
        '''
        Generate the Dockerfile file and all the other related files.

        props - The DefinitionProperties instance contianing information on what to
                put in the Dockerfile and which other files are needed.
        '''
        lines = []

        def emit(text=''):
            if not text.endswith('\n'):
                text += '\n'
            if text.startswith('\n') and len(text) > 1:
                text = text[1:]
            lines.append(textwrap.dedent(text))

        emit('# Generated by Karton.')
        emit()

        emit('FROM %s' % props.distro)
        emit()

        if props.maintainer:
            emit('MAINTAINER %s' % props.maintainer)
            emit()

        if props.additional_archs:
            archs_run = ['RUN \\']
            for i, arch in enumerate(props.additional_archs):
                if i == len(props.additional_archs) - 1:
                    cont = ''
                else:
                    cont = ' && \\'
                archs_run.append('    dpkg --add-architecture %s%s' % (arch, cont))
            archs_run.append('')
            archs_run.append('')
            emit('\n'.join(archs_run))

        emit(
            r'''
            RUN \
                export DEBIAN_FRONTEND=noninteractive && \
                apt-get update -qqy && \
                apt-get install -qqy -o=Dpkg::Use-Pty=0 \
                    --no-install-recommends \
                    apt-utils \
                    && \
                apt-get install -qqy -o=Dpkg::Use-Pty=0 \
                    locales
            ''')

        props.packages.append('python')

        if props.packages:
            packages = ' '.join(props.packages)
            emit(
                r'''
                RUN \
                    export DEBIAN_FRONTEND=noninteractive && \
                    sed -e 's|^# en_GB.UTF-8|en_GB.UTF-8|g' -i /etc/locale.gen && \
                    locale-gen && \
                    TERM=xterm apt-get install -qqy -o=Dpkg::Use-Pty=0 \
                        %s
                ''' % packages)

        emit(
            r'''
            RUN \
                apt-get clean -qq
            ''')

        emit(
            r'''
            RUN \
                mkdir -p $(dirname %(user_home)s) && \
                useradd -m -s /bin/bash --home-dir %(user_home)s %(username)s && \
                chown %(username)s %(user_home)s
            ENV USER %(username)s
            USER %(username)s
            '''
            % dict(
                username=props.username,
                user_home=props.user_home,
                ))

        container_code_path = os.path.join(dirs.root_code_dir(), 'container-code')
        for container_script in ('session_runner.py',):
            path = os.path.join(container_code_path, container_script)
            copyable_path = self._make_file_copyable(path)
            emit('ADD %s /karton/%s' %
                 (copyable_path, container_script))
        emit()

        content = ''.join(lines).strip() + '\n'
        verbose('The Dockerfile is:\n========\n%s========' % content)

        with open(os.path.join(self._dst_dir, 'Dockerfile'), 'w') as output:
            output.write(content)

    def cleanup(self):
        '''
        Removes intermediate files created to build the image.
        '''
        shutil.rmtree(self._dst_dir)
