"""
This script is part of the pytest backport process which is triggered by comments
in pull requests.

This script is started by the `backport-on-comment.yml` workflow, which is triggered by two comment
related events:

* https://help.github.com/en/actions/reference/events-that-trigger-workflows#issue-comment-event-issue_comment
* https://help.github.com/en/actions/reference/events-that-trigger-workflows#issues-event-issues

This script receives the payload and a secrets on the command line.

The payload must contain a comment with a phrase matching this pseudo-regular expression:

    @pytestbot please backport to <branch name>

where branch name is a stable branch name, e.g. `6.0.x`.

Then it will create a pull request with a backport of the merge commit to the branch.

**Secret**: currently the secret is defined in the @pytestbot account, which the core maintainers
have access to. There we created a new secret named `chatops` with write access to the repository.
"""
import argparse
import json
import os
import re
from pathlib import Path
from subprocess import CalledProcessError
from subprocess import run
from textwrap import dedent
from typing import Any
from typing import Dict
from typing import Optional
from typing import Tuple

from colorama import Fore
from colorama import init
from github3.repos import Repository


SLUG = "pytest-dev/pytest"

PR_BODY = """\
Created automatically from {comment_url}.

Backport of PR #{pull_id} to `{target_branch}`.
"""


def login(token: str) -> Repository:
    import github3

    github = github3.login(token=token)
    owner, repo = SLUG.split("/")
    return github.repository(owner, repo)


def validate_and_get_issue_comment_payload(
    issue_payload_path: Path,
) -> Tuple[Dict[str, Any], Optional[str]]:
    payload = json.loads(issue_payload_path.read_text(encoding="UTF-8"))
    body = payload["comment"]["body"]
    m = re.match(r"@pytestbot please backport to ([\w\-_\.]+)", body)
    if m:
        target_branch = m.group(1)
    else:
        target_branch = None
    return payload, target_branch


def print_and_exit(msg) -> None:
    print(msg)
    raise SystemExit(1)


def trigger_backport(payload_path: Path, token: str) -> None:
    error_contents = ""  # to be used to store error output in case any command fails
    payload, target_branch = validate_and_get_issue_comment_payload(payload_path)
    if target_branch is None:
        url = payload["comment"]["html_url"]
        print_and_exit(
            f"Comment {Fore.CYAN}{url}{Fore.RESET} did not match the trigger command."
        )
    print()
    print(f"Precessing backport to branch {Fore.CYAN}{target_branch}")

    repo = login(token)

    issue_number = payload["issue"]["number"]
    issue = repo.issue(issue_number)

    run(["git", "checkout", f"origin/{target_branch}"], check=True)

    pull_id = payload["issue"]["id"]
    pull_title = payload["issue"]["title"]
    pull = repo.pull_request(pull_id)

    try:
        backport_branch = f"backport-{pull_id}"

        run(
            ["git", "config", "user.name", "pytest bot"],
            text=True,
            check=True,
            capture_output=True,
        )
        run(
            ["git", "config", "user.email", "pytestbot@gmail.com"],
            text=True,
            check=True,
            capture_output=True,
        )

        run(
            ["git", "checkout", "-b", backport_branch, f"origin/{target_branch}"],
            text=True,
            check=True,
            capture_output=True,
        )

        print(f"Branch {Fore.CYAN}{backport_branch}{Fore.RESET} created.")

        run(
            ["git", "cherry-pick", "-x", "-m1", pull.merge_commit_sha],
            text=True,
            check=True,
            capture_output=True,
        )

        oauth_url = f"https://{token}:x-oauth-basic@github.com/{SLUG}.git"
        run(
            ["git", "push", oauth_url, f"HEAD:{backport_branch}", "--force"],
            text=True,
            check=True,
            capture_output=True,
        )
        print(f"Branch {Fore.CYAN}{backport_branch}{Fore.RESET} pushed.")

        body = PR_BODY.format(
            comment_url=payload["comment"]["html_url"],
            pull_id=pull_id,
            target_branch=target_branch,
        )
        pr = repo.create_pull(
            f"[{target_branch}] {pull_title}",
            base=target_branch,
            head=backport_branch,
            body=body,
        )
        print(f"Pull request {Fore.CYAN}{pr.url}{Fore.RESET} created.")

        comment = issue.create_comment(
            f"As requested, opened a PR for backport to `{target_branch}`: #{pr.number}."
        )
        print(f"Notified in original comment {Fore.CYAN}{comment.url}{Fore.RESET}.")

        print(f"{Fore.GREEN}Success.")
    except CalledProcessError as e:
        error_contents = e.output
    except Exception as e:
        error_contents = str(e)
        link = f"https://github.com/{SLUG}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
        issue.create_comment(
            dedent(
                f"""
            Sorry, the request to backport PR #{pull_id} to `{target_branch}` failed with:

            ```
            {e}
            ```

            See: {link}.
            """
            )
        )
        print_and_exit(f"{Fore.RED}{e}")

    if error_contents:
        link = f"https://github.com/{SLUG}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
        issue.create_comment(
            dedent(
                f"""
                Sorry, the request to backport PR #{pull_id} to `{target_branch}` failed with:

                ```
                {error_contents}
                ```

                See: {link}.
                """
            )
        )
        print_and_exit(f"{Fore.RED}{error_contents}")


def main() -> None:
    init(autoreset=True)
    parser = argparse.ArgumentParser()
    parser.add_argument("payload")
    parser.add_argument("token")
    options = parser.parse_args()
    trigger_backport(Path(options.payload), options.token)


if __name__ == "__main__":
    main()
