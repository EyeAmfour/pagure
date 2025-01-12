Jenkins with Pagure-ci
======================

Jenkins is a Continuous Integration service that can be configured to be
integrated with Pagure.

This document describe the steps needed to make it work.

How does it work?
-----------------

The principal is:

* Pagure will trigger a build on jenkins when a pull-request is created,
  updated or when someone explicitly asks Pagure to do so or when a new commit
  is pushed (if pagure-ci is configured to trigger on commit).

* Pagure will send a few information to jenkins when triggering a build:
  ``REPO``, ``BRANCH``, ``BRANCH_TO``, ``cause``.

* jenkins will do its work and, using webhook, report to Pagure that it has
  finished its task

* Pagure will query jenkins to know the outcome of the task and flag the PR
  accordingly

``REPO`` corresponds to the url of the repository the pull-request originates
from (so most often it will be a fork of the main repository).

``BRANCH`` corresponds to the branch the pull-request originates from (the
branch of the fork).

``BRANCH_TO`` corresponds to the targeted branch in the main repository (the
branch of the main project in which the PR is to be merged).

``cause`` is the reason the build was triggered (ie: the pull-request id or the
commit hash).


How to enable Pagure CI
-----------------------

* Visit the settings page of your project

* Scroll down to the `Hooks` section and click on `Pagure CI`

* Select the type of CI service you want

* Enter the URL of the CI service. For example `http://jenkins.fedoraproject.org`

* Enter the name of the job the CI service will trigger. For example `pagure-ci`

* Tick the checkbox activating the hook. Either trigger on every commits, trigger only
  on pull-requests or both every commits and pull-requests.


These steps will activate the hook, after reloading the page or the tab, you
will be given access to two important values: the token used to trigger the
build on jenkins and the URL used by jenkins to report the status of the
build.
Keep these two available when configuring jenkins for your project.


Configure Jenkins
-----------------

These steps can only be made by the admins of your jenkins instance, but
they only need to be made once.

* Download the following plugins:

  * `Git Plugin <https://wiki.jenkins-ci.org/display/JENKINS/Git+Plugin>`_
  * `Notification Plugin <https://wiki.jenkins-ci.org/display/JENKINS/Notification+Plugin>`_


Configure your project on Jenkins
---------------------------------

* Go to the `Configure` page of your project

* Under `Job Notification`  click `Add Endpoint`

* Fields in Endpoint will be :

::

    FORMAT: JSON
    PROTOCOL: HTTP
    EVENT: All Events
    URL: <The URL provided in the Pagure CI hook on Pagure>
    TIMEOUT: 3000
    LOG: 1

* Tick the checkbox `This build is parameterized`

* Add two `String Parameters` named REPO and BRANCH

* Source Code Management select Git  and give the URL of the Pagure project

* Under Build Trigger click on Trigger build remotely and specify the token
  given by Pagure.

* Under Build -> Add build step -> Execute Shell

* In the box given  enter the shell steps you want for testing your project.


Example Script

::

    # Script specific for Pull-Request build
    if [ -n "$REPO" -a -n "$BRANCH" ]; then
    git remote rm proposed || true
    git remote add proposed "$REPO"
    git fetch proposed
    git checkout origin/master
    git config --global user.email "you@example.com"
    git config --global user.name "Your Name"
    git merge --no-ff "proposed/$BRANCH" -m "Merge PR"
    fi

    # Part of the script specific to how you run the tests on your project
