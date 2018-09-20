# -*- coding: utf-8 -*-

"""
 (c) 2015-2017 - Copyright Red Hat Inc

 Authors:
   Pierre-Yves Chibon <pingou@pingoured.fr>

Internal endpoints.

"""

from __future__ import unicode_literals

import collections
import logging
import os

import flask
import pygit2

from functools import wraps
from sqlalchemy.exc import SQLAlchemyError

PV = flask.Blueprint("internal_ns", __name__, url_prefix="/pv")

import pagure  # noqa: E402
import pagure.exceptions  # noqa: E402
import pagure.forms  # noqa: E402
import pagure.lib  # noqa: E402
import pagure.lib.git  # noqa: E402
import pagure.lib.tasks  # noqa: E402
import pagure.utils  # noqa: E402
import pagure.ui.fork  # noqa: E402


_log = logging.getLogger(__name__)


MERGE_OPTIONS = {
    "NO_CHANGE": {
        "short_code": "No changes",
        "message": "Nothing to change, git is up to date",
    },
    "FFORWARD": {
        "short_code": "Ok",
        "message": "The pull-request can be merged and fast-forwarded",
    },
    "CONFLICTS": {
        "short_code": "Conflicts",
        "message": "The pull-request cannot be merged due to conflicts",
    },
    "MERGE": {
        "short_code": "With merge",
        "message": "The pull-request can be merged with a merge commit",
    },
}


def localonly(function):
    """ Decorator used to check if the request is local or not.
    """

    @wraps(function)
    def decorated_function(*args, **kwargs):
        """ Wrapped function actually checking if the request is local.
        """
        ip_allowed = pagure.config.config.get(
            "IP_ALLOWED_INTERNAL", ["127.0.0.1", "localhost", "::1"]
        )
        if flask.request.remote_addr not in ip_allowed:
            _log.debug(
                "IP: %s is not in the list of allowed IPs: %s"
                % (flask.request.remote_addr, ip_allowed)
            )
            flask.abort(403)
        else:
            return function(*args, **kwargs)

    return decorated_function


@PV.route("/pull-request/comment/", methods=["PUT"])
@localonly
def pull_request_add_comment():
    """ Add a comment to a pull-request.
    """
    pform = pagure.forms.ProjectCommentForm(csrf_enabled=False)
    if not pform.validate_on_submit():
        flask.abort(400, "Invalid request")

    objid = pform.objid.data
    useremail = pform.useremail.data

    request = pagure.lib.get_request_by_uid(flask.g.session, request_uid=objid)

    if not request:
        flask.abort(404, "Pull-request not found")

    form = pagure.forms.AddPullRequestCommentForm(csrf_enabled=False)

    if not form.validate_on_submit():
        flask.abort(400, "Invalid request")

    commit = form.commit.data or None
    tree_id = form.tree_id.data or None
    filename = form.filename.data or None
    row = form.row.data or None
    comment = form.comment.data

    try:
        message = pagure.lib.add_pull_request_comment(
            flask.g.session,
            request=request,
            commit=commit,
            tree_id=tree_id,
            filename=filename,
            row=row,
            comment=comment,
            user=useremail,
        )
        flask.g.session.commit()
    except SQLAlchemyError as err:  # pragma: no cover
        flask.g.session.rollback()
        _log.exception(err)
        flask.abort(500, "Error when saving the request to the database")

    return flask.jsonify({"message": message})


@PV.route("/ticket/comment/", methods=["PUT"])
@localonly
def ticket_add_comment():
    """ Add a comment to an issue.
    """
    pform = pagure.forms.ProjectCommentForm(csrf_enabled=False)
    if not pform.validate_on_submit():
        flask.abort(400, "Invalid request")

    objid = pform.objid.data
    useremail = pform.useremail.data

    issue = pagure.lib.get_issue_by_uid(flask.g.session, issue_uid=objid)

    if issue is None:
        flask.abort(404, "Issue not found")

    user_obj = pagure.lib.search_user(flask.g.session, email=useremail)
    admin = False
    if user_obj:
        admin = user_obj.user == issue.project.user.user or (
            user_obj.user in [user.user for user in issue.project.committers]
        )

    if (
        issue.private
        and user_obj
        and not admin
        and not issue.user.user == user_obj.username
    ):
        flask.abort(
            403, "This issue is private and you are not allowed to view it"
        )

    form = pagure.forms.CommentForm(csrf_enabled=False)

    if not form.validate_on_submit():
        flask.abort(400, "Invalid request")

    comment = form.comment.data

    try:
        message = pagure.lib.add_issue_comment(
            flask.g.session,
            issue=issue,
            comment=comment,
            user=useremail,
            notify=True,
        )
        flask.g.session.commit()
    except SQLAlchemyError as err:  # pragma: no cover
        flask.g.session.rollback()
        _log.exception(err)
        flask.abort(500, "Error when saving the request to the database")

    return flask.jsonify({"message": message})


@PV.route("/pull-request/merge", methods=["POST"])
def mergeable_request_pull():
    """ Returns if the specified pull-request can be merged or not.
    """
    force = flask.request.form.get("force", False)
    if force is not False:
        force = True

    form = pagure.forms.ConfirmationForm()
    if not form.validate_on_submit():
        response = flask.jsonify(
            {"code": "CONFLICTS", "message": "Invalid input submitted"}
        )
        response.status_code = 400
        return response

    requestid = flask.request.form.get("requestid")

    request = pagure.lib.get_request_by_uid(
        flask.g.session, request_uid=requestid
    )

    if not request:
        response = flask.jsonify(
            {"code": "CONFLICTS", "message": "Pull-request not found"}
        )
        response.status_code = 404
        return response

    merge_status = request.merge_status
    if not merge_status or force:
        try:
            merge_status = pagure.lib.git.merge_pull_request(
                session=flask.g.session,
                request=request,
                username=None,
                domerge=False,
            )
        except pygit2.GitError as err:
            response = flask.jsonify({"code": "CONFLICTS", "message": "%s" % err})
            response.status_code = 409
            return response
        except pagure.exceptions.PagureException as err:
            response = flask.jsonify({"code": "CONFLICTS", "message": "%s" % err})
            response.status_code = 500
            return response

    return flask.jsonify(
        pagure.utils.get_merge_options(request, merge_status)
    )


@PV.route("/pull-request/ready", methods=["POST"])
def get_pull_request_ready_branch():
    """ Return the list of branches that have commits not in the main
    branch/repo (thus for which one could open a PR) and the number of
    commits that differ.
    """
    form = pagure.forms.ConfirmationForm()
    if not form.validate_on_submit():
        response = flask.jsonify(
            {"code": "ERROR", "message": "Invalid input submitted"}
        )
        response.status_code = 400
        return response

    repo = pagure.lib.get_authorized_project(
        flask.g.session,
        flask.request.form.get("repo", "").strip() or None,
        namespace=flask.request.form.get("namespace", "").strip() or None,
        user=flask.request.form.get("repouser", "").strip() or None,
    )

    if not repo:
        response = flask.jsonify(
            {
                "code": "ERROR",
                "message": "No repo found with the information provided",
            }
        )
        response.status_code = 404
        return response

    reponame = pagure.utils.get_repo_path(repo)
    repo_obj = pygit2.Repository(reponame)
    if repo.is_fork and repo.parent:
        if not repo.parent.settings.get("pull_requests", True):
            response = flask.jsonify(
                {
                    "code": "ERROR",
                    "message": "Pull-request have been disabled for this repo",
                }
            )
            response.status_code = 400
            return response

        parentreponame = pagure.utils.get_repo_path(repo.parent)
        parent_repo_obj = pygit2.Repository(parentreponame)
    else:
        if not repo.settings.get("pull_requests", True):
            response = flask.jsonify(
                {
                    "code": "ERROR",
                    "message": "Pull-request have been disabled for this repo",
                }
            )
            response.status_code = 400
            return response

        parent_repo_obj = repo_obj

    branches = {}
    if not repo_obj.is_empty and len(repo_obj.listall_branches()) > 0:
        for branchname in repo_obj.listall_branches():
            compare_branch = None
            if (
                not parent_repo_obj.is_empty
                and not parent_repo_obj.head_is_unborn
            ):
                try:
                    if pagure.config.config.get(
                        "PR_TARGET_MATCHING_BRANCH", False
                    ):
                        # find parent branch which is the longest substring of
                        # branch that we're processing
                        compare_branch = ""
                        for parent_branch in parent_repo_obj.branches:
                            if (
                                not repo.is_fork
                                and branchname == parent_branch
                            ):
                                continue
                            if branchname.startswith(parent_branch) and len(
                                parent_branch
                            ) > len(compare_branch):
                                compare_branch = parent_branch
                        compare_branch = (
                            compare_branch or repo_obj.head.shorthand
                        )
                    else:
                        compare_branch = repo_obj.head.shorthand
                except pygit2.GitError:
                    pass  # let compare_branch be None

            # Do not compare a branch to itself
            if (
                not repo.is_fork
                and compare_branch
                and compare_branch == branchname
            ):
                continue

            diff_commits = None
            try:
                _, diff_commits, _ = pagure.lib.git.get_diff_info(
                    repo_obj, parent_repo_obj, branchname, compare_branch
                )
            except pagure.exceptions.PagureException:
                pass

            if diff_commits:
                branches[branchname] = {
                    "commits": [c.oid.hex for c in diff_commits],
                    "target_branch": compare_branch or "master",
                }

    prs = pagure.lib.search_pull_requests(
        flask.g.session, project_id_from=repo.id, status="Open"
    )
    branches_pr = {}
    for pr in prs:
        if pr.branch_from in branches:
            branches_pr[pr.branch_from] = "%s/pull-request/%s" % (
                pr.project.url_path,
                pr.id,
            )
            del (branches[pr.branch_from])

    return flask.jsonify(
        {
            "code": "OK",
            "message": {"new_branch": branches, "branch_w_pr": branches_pr},
        }
    )


@PV.route("/<repo>/issue/template", methods=["POST"])
@PV.route("/<namespace>/<repo>/issue/template", methods=["POST"])
@PV.route("/fork/<username>/<repo>/issue/template", methods=["POST"])
@PV.route(
    "/fork/<username>/<namespace>/<repo>/issue/template", methods=["POST"]
)
def get_ticket_template(repo, namespace=None, username=None):
    """ Return the template asked for the specified project
    """

    form = pagure.forms.ConfirmationForm()
    if not form.validate_on_submit():
        response = flask.jsonify(
            {"code": "ERROR", "message": "Invalid input submitted"}
        )
        response.status_code = 400
        return response

    template = flask.request.args.get("template", None)
    if not template:
        response = flask.jsonify(
            {"code": "ERROR", "message": "No template provided"}
        )
        response.status_code = 400
        return response

    repo = pagure.lib.get_authorized_project(
        flask.g.session, repo, user=username, namespace=namespace
    )

    if not repo.settings.get("issue_tracker", True):
        response = flask.jsonify(
            {
                "code": "ERROR",
                "message": "No issue tracker found for this project",
            }
        )
        response.status_code = 404
        return response

    ticketrepopath = repo.repopath("tickets")
    content = None
    if os.path.exists(ticketrepopath):
        ticketrepo = pygit2.Repository(ticketrepopath)
        if not ticketrepo.is_empty and not ticketrepo.head_is_unborn:
            commit = ticketrepo[ticketrepo.head.target]
            # Get the asked template
            content_file = pagure.utils.__get_file_in_tree(
                ticketrepo,
                commit.tree,
                ["templates", "%s.md" % template],
                bail_on_tree=True,
            )
            if content_file:
                content, _ = pagure.doc_utils.convert_readme(
                    content_file.data, "md"
                )
    if content:
        response = flask.jsonify({"code": "OK", "message": content})
    else:
        response = flask.jsonify(
            {"code": "ERROR", "message": "No such template found"}
        )
        response.status_code = 404
    return response


@PV.route("/branches/commit/", methods=["POST"])
def get_branches_of_commit():
    """ Return the list of branches that have the specified commit in
    """
    form = pagure.forms.ConfirmationForm()
    if not form.validate_on_submit():
        response = flask.jsonify(
            {"code": "ERROR", "message": "Invalid input submitted"}
        )
        response.status_code = 400
        return response

    commit_id = flask.request.form.get("commit_id", "").strip() or None
    if not commit_id:
        response = flask.jsonify(
            {"code": "ERROR", "message": "No commit id submitted"}
        )
        response.status_code = 400
        return response

    repo = pagure.lib.get_authorized_project(
        flask.g.session,
        flask.request.form.get("repo", "").strip() or None,
        user=flask.request.form.get("repouser", "").strip() or None,
        namespace=flask.request.form.get("namespace", "").strip() or None,
    )

    if not repo:
        response = flask.jsonify(
            {
                "code": "ERROR",
                "message": "No repo found with the information provided",
            }
        )
        response.status_code = 404
        return response

    repopath = repo.repopath("main")

    if not os.path.exists(repopath):
        response = flask.jsonify(
            {
                "code": "ERROR",
                "message": "No git repo found with the information provided",
            }
        )
        response.status_code = 404
        return response

    repo_obj = pygit2.Repository(repopath)

    try:
        commit_id in repo_obj
    except ValueError:
        response = flask.jsonify(
            {
                "code": "ERROR",
                "message": "This commit could not be found in this repo",
            }
        )
        response.status_code = 404
        return response

    branches = []
    if not repo_obj.head_is_unborn:
        compare_branch = repo_obj.lookup_branch(repo_obj.head.shorthand)
    else:
        compare_branch = None

    for branchname in repo_obj.listall_branches():
        branch = repo_obj.lookup_branch(branchname)

        if not repo_obj.is_empty and len(repo_obj.listall_branches()) > 1:

            merge_commit = None

            if compare_branch:
                merge_commit_obj = repo_obj.merge_base(
                    compare_branch.get_object().hex, branch.get_object().hex
                )

                if merge_commit_obj:
                    merge_commit = merge_commit_obj.hex

            repo_commit = repo_obj[branch.get_object().hex]

            for commit in repo_obj.walk(
                repo_commit.oid.hex, pygit2.GIT_SORT_TIME
            ):
                if commit.oid.hex == merge_commit:
                    break
                if commit.oid.hex == commit_id:
                    branches.append(branchname)
                    break

    # If we didn't find the commit in any branch and there is one, then it
    # is in the default branch.
    if not branches and compare_branch:
        branches.append(compare_branch.branch_name)

    return flask.jsonify({"code": "OK", "branches": branches})


@PV.route("/branches/heads/", methods=["POST"])
def get_branches_head():
    """ Return the heads of each branch in the repo, using the following
    structure:
    {
        code: 'OK',
        branches: {
            name : commit,
            ...
        },
        heads: {
            commit : [branch, ...],
            ...
        }
    }
    """
    form = pagure.forms.ConfirmationForm()
    if not form.validate_on_submit():
        response = flask.jsonify(
            {"code": "ERROR", "message": "Invalid input submitted"}
        )
        response.status_code = 400
        return response

    repo = pagure.lib.get_authorized_project(
        flask.g.session,
        flask.request.form.get("repo", "").strip() or None,
        namespace=flask.request.form.get("namespace", "").strip() or None,
        user=flask.request.form.get("repouser", "").strip() or None,
    )

    if not repo:
        response = flask.jsonify(
            {
                "code": "ERROR",
                "message": "No repo found with the information provided",
            }
        )
        response.status_code = 404
        return response

    repopath = repo.repopath("main")

    if not os.path.exists(repopath):
        response = flask.jsonify(
            {
                "code": "ERROR",
                "message": "No git repo found with the information provided",
            }
        )
        response.status_code = 404
        return response

    repo_obj = pygit2.Repository(repopath)

    branches = {}
    if not repo_obj.is_empty and len(repo_obj.listall_branches()) > 1:
        for branchname in repo_obj.listall_branches():
            branch = repo_obj.lookup_branch(branchname)
            branches[branchname] = branch.get_object().hex

    # invert the dict
    heads = collections.defaultdict(list)
    for branch, commit in branches.items():
        heads[commit].append(branch)

    return flask.jsonify({"code": "OK", "branches": branches, "heads": heads})


@PV.route("/task/<taskid>", methods=["GET"])
def task_info(taskid):
    """ Return the results of the specified task or a 418 if the task is
    still being processed.
    """
    task = pagure.lib.tasks.get_result(taskid)

    if task.ready():
        result = task.get(timeout=0, propagate=False)
        if isinstance(result, Exception):
            result = "%s" % result
        return flask.jsonify({"results": result})
    else:
        flask.abort(418)


@PV.route("/stats/commits/authors", methods=["POST"])
def get_stats_commits():
    """ Return statistics about the commits made on the specified repo.

    """
    form = pagure.forms.ConfirmationForm()
    if not form.validate_on_submit():
        response = flask.jsonify(
            {"code": "ERROR", "message": "Invalid input submitted"}
        )
        response.status_code = 400
        return response

    repo = pagure.lib.get_authorized_project(
        flask.g.session,
        flask.request.form.get("repo", "").strip() or None,
        namespace=flask.request.form.get("namespace", "").strip() or None,
        user=flask.request.form.get("repouser", "").strip() or None,
    )

    if not repo:
        response = flask.jsonify(
            {
                "code": "ERROR",
                "message": "No repo found with the information provided",
            }
        )
        response.status_code = 404
        return response

    repopath = repo.repopath("main")

    task = pagure.lib.tasks.commits_author_stats.delay(repopath)

    return flask.jsonify(
        {
            "code": "OK",
            "message": "Stats asked",
            "url": flask.url_for("internal_ns.task_info", taskid=task.id),
            "task_id": task.id,
        }
    )


@PV.route("/stats/commits/trend", methods=["POST"])
def get_stats_commits_trend():
    """ Return evolution of the commits made on the specified repo.

    """
    form = pagure.forms.ConfirmationForm()
    if not form.validate_on_submit():
        response = flask.jsonify(
            {"code": "ERROR", "message": "Invalid input submitted"}
        )
        response.status_code = 400
        return response

    repo = pagure.lib.get_authorized_project(
        flask.g.session,
        flask.request.form.get("repo", "").strip() or None,
        namespace=flask.request.form.get("namespace", "").strip() or None,
        user=flask.request.form.get("repouser", "").strip() or None,
    )

    if not repo:
        response = flask.jsonify(
            {
                "code": "ERROR",
                "message": "No repo found with the information provided",
            }
        )
        response.status_code = 404
        return response

    repopath = repo.repopath("main")

    task = pagure.lib.tasks.commits_history_stats.delay(repopath)

    return flask.jsonify(
        {
            "code": "OK",
            "message": "Stats asked",
            "url": flask.url_for("internal_ns.task_info", taskid=task.id),
            "task_id": task.id,
        }
    )


@PV.route("/<repo>/family", methods=["POST"])
@PV.route("/<namespace>/<repo>/family", methods=["POST"])
@PV.route("/fork/<username>/<repo>/family", methods=["POST"])
@PV.route("/fork/<username>/<namespace>/<repo>/family", methods=["POST"])
def get_project_family(repo, namespace=None, username=None):
    """ Return the family of projects for the specified project

    {
        code: 'OK',
        family: [
        ]
    }
    """

    allows_pr = flask.request.form.get("allows_pr", "").lower().strip() in [
        "1",
        "true",
    ]
    allows_issues = flask.request.form.get(
        "allows_issues", ""
    ).lower().strip() in ["1", "true"]

    form = pagure.forms.ConfirmationForm()
    if not form.validate_on_submit():
        response = flask.jsonify(
            {"code": "ERROR", "message": "Invalid input submitted"}
        )
        response.status_code = 400
        return response

    repo = pagure.lib.get_authorized_project(
        flask.g.session, repo, user=username, namespace=namespace
    )

    if not repo:
        response = flask.jsonify(
            {
                "code": "ERROR",
                "message": "No repo found with the information provided",
            }
        )
        response.status_code = 404
        return response

    if allows_pr:
        family = [
            p.url_path
            for p in pagure.lib.get_project_family(flask.g.session, repo)
            if p.settings.get("pull_requests", True)
        ]
    elif allows_issues:
        family = [
            p.url_path
            for p in pagure.lib.get_project_family(flask.g.session, repo)
            if p.settings.get("issue_tracker", True)
        ]
    else:
        family = [
            p.url_path
            for p in pagure.lib.get_project_family(flask.g.session, repo)
        ]

    return flask.jsonify({"code": "OK", "family": family})
