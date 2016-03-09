#!/usr/bin/env python

import getpass
import os
import subprocess
import sys

from collections import defaultdict

import fedmsg
import fedmsg.config


if 'PAGURE_CONFIG' not in os.environ \
        and os.path.exists('/etc/pagure/pagure.cfg'):
    os.environ['PAGURE_CONFIG'] = '/etc/pagure/pagure.cfg'

import pagure
import pagure.lib.git

abspath = os.path.abspath(os.environ['GIT_DIR'])


print "Emitting a message to the fedmsg bus."
config = fedmsg.config.load_config([], None)
config['active'] = True
config['endpoints']['relay_inbound'] = config['relay_inbound']
fedmsg.init(name='relay_inbound', **config)


def build_stats(commit):
    cmd = ['diff-tree', '--numstat', '%s' % (commit)]
    output = pagure.lib.git.read_git_lines(cmd, abspath)

    files = {}
    total = {}
    for line in output[1:]:
        additions, deletions, path = line.split('\t')
        additions, deletions = additions.strip(), deletions.strip()

        try:
            additions = int(additions)
        except ValueError:
            additions = 0

        try:
            deletions = int(deletions)
        except ValueError:
            deletions = 0

        path = path.strip()
        files[path] = {
            'additions':  additions,
            'deletions': deletions,
            'lines': additions + deletions,
        }

    total = defaultdict(int)
    for name, stats in files.items():
        total['additions'] += stats['additions']
        total['deletions'] += stats['deletions']
        total['lines'] += stats['lines']
        total['files'] += 1

    return files, total


seen = []

# Read in all the rev information git-receive-pack hands us.
for line in sys.stdin.readlines():
    (oldrev, newrev, refname) = line.strip().split(' ', 2)

    forced = False
    if set(newrev) == set(['0']):
        print "Deleting a reference/branch, so we won't run the "\
            "pagure hook"
        break
    elif set(oldrev) == set(['0']):
        print "New reference/branch"
        oldrev = '^%s' % oldrev
    elif pagure.lib.git.is_forced_push(oldrev, newrev, abspath):
        forced = True
        base = pagure.lib.git.get_base_revision(oldrev, newrev, abspath)
        if base:
            oldrev = base[0]

    revs = pagure.lib.git.get_revs_between(
        oldrev, newrev, abspath, forced=forced)
    project_name = pagure.lib.git.get_repo_name(abspath)
    username = pagure.lib.git.get_username(abspath)
    project = pagure.lib.get_project(pagure.SESSION, project_name, username)
    if not project:
        project = project_name

    def _build_commit(rev):
        files, total = build_stats(rev)

        summary = pagure.lib.git.read_git_lines(
            ['log', '-1', rev, "--pretty='%s'"],
            abspath)[0].replace("'", '')
        message = pagure.lib.git.read_git_lines(
            ['log', '-1', rev, "--pretty='%B'"],
            abspath)[0].replace("'", '')

        return dict(
            name=pagure.lib.git.get_pusher(rev, abspath),
            email=pagure.lib.git.get_pusher_email(rev, abspath),
            summary=summary,
            message=message,
            stats=dict(
                files=files,
                total=total,
            ),
            rev=unicode(rev),
            path=abspath,
            username=username,
            agent=os.getlogin(),
        )

    commits = map(_build_commit, revs)

    final_commits = []
    for commit in reversed(commits):
        if commit is None:
            continue

        # Keep track of whether or not we have already published this commit
        # on another branch or not.  It is conceivable that someone could
        # make a commit to a number of branches, and push them all at the
        # same time.
        # Make a note in the fedmsg payload so we can try to reduce spam at
        # a later stage.
        if commit['rev'] in seen:
            commit['seen'] = True
        else:
            commit['seen'] = False
            seen.append(commit['rev'])
        final_commits.append(commit)

    if final_commits:
        print "* Publishing information for %i commits" % len(commits)
        pagure.lib.notify.log(
            project=project,
            topic="git.receive",
            msg=dict(
                commits=final_commits,
                branch=refname,
                forced=forced,
                agent=username,
                repo=project.to_json(public=True)
                if not isinstance(project, basestring) else project,
            ),
        )
