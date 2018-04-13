import os
import shutil
import logging
from contextlib import redirect_stdout
from ...util import run, apply_patch, qjoin, FatalError
from ...target import Target
from ...packages import Nothp


class SPEC2006(Target):
    name = 'spec2006'

    def __init__(self, specdir=None, giturl=None, patches=[],
                 nothp=True, force_cpu=0):
        if not specdir and not giturl:
            raise FatalError('should specify one of specdir or giturl')

        if specdir and giturl:
            raise FatalError('cannot specify specdir AND giturl')

        self.specdir = specdir
        self.giturl = giturl
        self.patches = patches
        self.nothp = nothp
        self.force_cpu = force_cpu

        # TODO: 4 options for fetching:
        # - git repo
        # - tarfile
        # - source dir to install from
        # - existing install dir

    def add_build_args(self, parser, desc='build'):
        parser.add_argument('--spec2006-benchmarks',
                nargs='+', metavar='BENCHMARK', default=['c', 'c++'],
                choices=list(self.benchmarks.keys()),
                help='which SPEC-CPU2006 benchmarks to build')

    def add_run_args(self, parser):
        parser.add_argument('--benchmarks', '--spec2006-benchmarks',
                dest='spec2006_benchmarks',
                nargs='+', metavar='BENCHMARK', default=[],
                choices=list(self.benchmarks.keys()),
                help='which benchmarks to run')
        parser.add_argument('--measuremem', action='store_true',
                help='measure memory usage (single run, does not support '
                     'runspec arguments)')

    def dependencies(self):
        if self.nothp:
            yield Nothp()

    def is_fetched(self, ctx):
        return os.path.exists('install/shrc')

    def fetch(self, ctx):
        if self.giturl:
            ctx.log.debug('cloning SPEC2006 repo')
            run(ctx, ['git', 'clone', '--depth', 1, self.giturl, 'src'])
            os.chdir('src')
        else:
            os.chdir(self.specdir)

        install_path = self.path(ctx, 'install')
        ctx.log.debug('installing SPEC2006 into ' + install_path)
        run(ctx, ['./install.sh', '-f', '-d', install_path],
            env={'PERL_TEST_NUMCONVERTS': 1})

        if self.giturl:
            ctx.log.debug('removing cloned SPEC2006 repo to save disk space')
            shutil.rmtree(self.path(ctx, 'src'))

    def build(self, ctx, instance):
        # apply any pending patches (doing this at build time allows adding
        # patches during instance development)
        os.chdir('install')
        config_root = os.path.dirname(os.path.abspath(__file__))
        for path in self.patches:
            if '/' not in path:
                path = '%s/%s.patch' % (config_root, path)
            apply_patch(ctx, path, 1)
        os.chdir('..')

        config = self.make_spec_config(ctx, instance)
        print_output = ctx.loglevel == logging.DEBUG

        for bench in self.get_benchmarks(ctx, instance):
            ctx.log.info('building %s-%s %s' % (self.name, instance.name, bench))
            self.run_bash(ctx,
                'killwrap_tree runspec --config=%s --action=build %s' %
                (config, bench), teeout=print_output)

    def run(self, ctx, instance):
        config = 'infra-' + instance.name
        config_root = os.path.dirname(os.path.abspath(__file__))

        if not os.path.exists(self.path(ctx, 'install/config/%s.cfg' % config)):
            raise FatalError('%s-%s has not been built yet!' %
                             (self.name, instance.name))

        runspec_args = self.get_benchmarks(ctx, instance)
        # TODO: other args

        wrapper =  'killwrap_tree'
        if self.nothp:
            wrapper += ' nothp'
        if self.force_cpu >= 0:
            wrapper += ' taskset -c %d' % self.force_cpu

        if ctx.args.measuremem:
            specdir = self.path(ctx, 'install')
            self.run_bash(ctx,
                'runspec --config={config} --action=setup {runspec_args};'
                '{wrapper} {config_root}/measuremem.py {specdir} {config}'
                ' {benchmarks}'.format(**locals()),
                teeout=True)
        else:
            self.run_bash(ctx,
                '%s runspec --config=%s --nobuild %s' %
                (wrapper, config, qjoin(runspec_args)),
                teeout=True)

    def run_bash(self, ctx, commands, **kwargs):
        config_root = os.path.dirname(os.path.abspath(__file__))
        return run(ctx, [
            'bash', '-c',
            'cd %s/install;'
            'source shrc;'
            'source "%s/scripts/kill-tree-on-interrupt.inc";'
            '%s' %
            (self.path(ctx), config_root, commands)
        ], **kwargs)

    def make_spec_config(self, ctx, instance):
        config_name = 'infra-' + instance.name
        config_path = self.path(ctx, 'install/config/%s.cfg' % config_name)
        ctx.log.debug('writing SPEC2006 config to ' + config_path)

        with open(config_path, 'w') as f:
            with redirect_stdout(f):
                print('tune        = base')
                print('ext         = ' + config_name)
                print('reportable  = no')
                print('teeout      = yes')
                print('teerunout   = no')
                print('makeflags   = -j%d' % ctx.jobs)
                print('strict_rundir_verify = no')
                print('')
                print('default=default=default=default:')

                # see https://www.spec.org/cpu2006/Docs/makevars.html#nofbno1
                # for flags ordering
                cflags = qjoin(ctx.cflags)
                cxxflags = qjoin(ctx.cxxflags)
                ldflags = qjoin(ctx.ldflags)
                print('CC          = %s %s' % (ctx.cc, cflags))
                print('CXX         = %s %s' % (ctx.cxx, cxxflags))
                print('FC          = `which false`')
                print('CLD         = %s %s' % (ctx.cc, ldflags))
                print('CXXLD       = %s %s' % (ctx.cxx, ldflags))
                print('COPTIMIZE   = -std=gnu89')
                print('CXXOPTIMIZE = -std=c++98') # fix __float128 in old clang

                # post-build hooks call back into the setup script
                if ctx.hooks.post_build:
                    print('')
                    print('build_post_bench = %s exec-hook post-build %s '
                        '`echo ${commandexe} | sed "s/_\\[a-z0-9\\]\\\\+\\\\.%s\\\\\\$//"`' %
                        (ctx.paths.setup, instance.name, config_name))
                    print('')

                if 'target_run_wrapper' in ctx:
                    print('')
                    print('monitor_wrapper = %s \$command' % ctx.target_run_wrapper)

                # configure benchmarks for 64-bit Linux (hardcoded for now)
                print('')
                print('default=base=default=default:')
                print('PORTABILITY    = -DSPEC_CPU_LP64')
                print('')
                print('400.perlbench=default=default=default:')
                print('CPORTABILITY   = -DSPEC_CPU_LINUX_X64')
                print('')
                print('462.libquantum=default=default=default:')
                print('CPORTABILITY   = -DSPEC_CPU_LINUX')
                print('')
                print('483.xalancbmk=default=default=default:')
                print('CXXPORTABILITY = -DSPEC_CPU_LINUX')
                print('')
                print('481.wrf=default=default=default:')
                print('wrf_data_header_size = 8')
                print('CPORTABILITY   = -DSPEC_CPU_CASE_FLAG -DSPEC_CPU_LINUX')

        return config_name

    def link(self, ctx, instance):
        pass

    # override post-build hook runner rather than defining `binary_paths` since
    # we add hooks to the generated SPEC config file and call them through the
    # exec-hook setup command instead
    def run_hooks_post_build(self, ctx, instance):
        pass

    def get_benchmarks(self, ctx, instance):
        benchmarks = set()
        for bset in ctx.args.spec2006_benchmarks:
            for bench in self.benchmarks[bset]:
                if not hasattr(instance, 'exclude_spec2006_benchmark') or \
                        not instance.exclude_spec2006_benchmark(bench):
                    benchmarks.add(bench)
        return sorted(benchmarks)

    # define benchmark sets
    # TODO: generate these from spec sets
    benchmarks = {
        'int': [
            '400.perlbench',
            '401.bzip2',
            '403.gcc',
            '429.mcf',
            '445.gobmk',
            '456.hmmer',
            '458.sjeng',
            '462.libquantum',
            '464.h264ref',
            '471.omnetpp',
            '473.astar',
            '483.xalancbmk'
        ],
        'fp': [
            '410.bwaves',
            '416.gamess',
            '433.milc',
            '434.zeusmp',
            '435.gromacs',
            '436.cactusADM',
            '437.leslie3d',
            '444.namd',
            '447.dealII',
            '450.soplex',
            '453.povray',
            '454.calculix',
            '459.GemsFDTD',
            '465.tonto',
            '470.lbm',
            '481.wrf',
            '482.sphinx3'
        ],
        'c': [
            '400.perlbench',
            '401.bzip2',
            '403.gcc',
            '429.mcf',
            '433.milc',
            '445.gobmk',
            '456.hmmer',
            '458.sjeng',
            '462.libquantum',
            '464.h264ref',
            '470.lbm',
            '482.sphinx3'
        ],
        'c++': [
            '444.namd',
            '447.dealII',
            '450.soplex',
            '453.povray',
            '471.omnetpp',
            '473.astar',
            '483.xalancbmk'
        ],
        'fortran': [
            '410.bwaves',
            '416.gamess',
            '434.zeusmp',
            '435.gromacs',
            '436.cactusADM',
            '437.leslie3d',
            '454.calculix',
            '459.GemsFDTD',
            '465.tonto',
            '481.wrf'
        ]
    }
    benchmarks['all'] = sorted(benchmarks['int'] + benchmarks['fp'])
    for bench in benchmarks['all']:
        benchmarks[bench] = [bench]
