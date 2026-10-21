# Contributing Guidelines

Thank you for your interest in contributing to our project. Whether it's a bug report, new feature, correction, or additional
documentation, we greatly value feedback and contributions from our community.

Please read through this document before submitting any issues or pull requests to ensure we have all the necessary
information to effectively respond to your bug report or contribution.


## Reporting Bugs/Feature Requests

We welcome you to use the GitHub issue tracker to report bugs or suggest features.

When filing an issue, please check existing open, or recently closed, issues to make sure somebody else hasn't already
reported the issue. Please try to include as much information as you can. Details like these are incredibly useful:

* A reproducible test case or series of steps
* The version of our code being used
* Any modifications you've made relevant to the bug
* Anything unusual about your environment or deployment


## Contributing via Pull Requests
Contributions via pull requests are much appreciated. Before sending us a pull request, please ensure that:

1. You are working against the latest source on the *main* branch.
2. You check existing open, and recently merged, pull requests to make sure someone else hasn't addressed the problem already.
3. You open an issue to discuss any significant work - we would hate for your time to be wasted.

To send us a pull request, please:

1. Fork the repository.
2. Modify the source; please focus on the specific change you are contributing. If you also reformat all the code, it will be hard for us to focus on your change.
3. Ensure local checks pass: `make lint test checks scan`. `uvx pre-commit install` runs the fast ones on every commit.
4. Commit to your fork using clear commit messages.
5. Send us a pull request, answering any default questions in the pull request interface.
6. Pay attention to any automated CI failures reported in the pull request, and stay involved in the conversation.

GitHub provides additional document on [forking a repository](https://help.github.com/articles/fork-a-repo/) and
[creating a pull request](https://help.github.com/articles/creating-a-pull-request/).

### Project-specific notes

* **Some modules are digest-pinned.** Each stage document records the digest of the rules that
  produced it, and `make checks` pins those digests. A change to a pinned module fails the
  checks until you update the pin, which is deliberate: it makes a rule change visible in review.
  Explain the behaviour change in the pull request when you update a pin.
* **Agent tool schemas are generated.** After changing a tool in `eks_addon_readiness/`, run
  `make generate` and commit the result.
* **Run `make fmt` before committing Terraform.**

### Local commands

```bash
make test       # unit tests
make checks     # every check lane, including the frozen-digest pins
make lint       # ruff and terraform fmt
make scan       # bandit and checkov (CI also runs tflint and gitleaks)
make generate   # regenerate agent prompts and tool schemas from the Python source
uvx pre-commit install   # optional: run the fast checks on every commit
```

### Repository layout

| Path | What it holds |
|---|---|
| `eks_addon_readiness/` | The pipeline: one package per stage concern (`scan`, `inventory`, `facts`, `decide`, `order`, `research`, `brief`, `upgrade_lane`, ...) |
| `entrypoints/` | Lambda entry points: the functions Terraform's `handler` strings name. One module per Lambda zip; each calls into `eks_addon_readiness/stages/` |
| `agents/` | The three agents' system prompts and harness configuration (generated) |
| `tools/` | Generators for the agents' prompts and tool schemas |
| `infra/terraform/deploy/` | The one Terraform root you apply |
| `infra/terraform/modules/` | `platform`, `research`, `brief`, `upgrade` |
| `infra/terraform/cluster-access/` | Per-cluster onboarding: a read-only role and access entry |
| `scripts/` | Package build and the upgrade lane's entry script |
| `tests/` | Unit tests and the check lanes |
| `docs/` | Install, architecture, security, cost, cleanup |

### Tests

The tests are the executable specification of what the agents *cannot* do. Each file names the
property it pins, so a regression surfaces as a failing property rather than a silent behaviour
change. A few that carry the load:

- `tests/test_the_release_gate_has_no_default_approver.py`: the release gate has no built-in default approver.
- `tests/test_the_approval_link_is_never_world_accessible.py`: the approval link is a signed, scoped URL.
- `tests/test_the_upgrade_agent_holds_no_write_path.py`: the upgrade agent has no tool that can change a cluster.
- `tests/test_a_quote_on_another_page_of_the_same_list_is_re_attributed.py`: a quote found on a sibling page is attributed to that page.
- `tests/test_one_brief_failing_does_not_drop_the_others.py`: one component's brief failing never discards the others.

A **digest pin** is a SHA-256 over the *source text* of the rule modules behind a stage, checked
against a frozen value. It is deliberately conservative: even a comment-only edit to a rule module
moves the digest and fails `make checks` until a reviewer confirms the rules are unchanged and
updates the pin.


## Finding contributions to work on
Looking at the existing issues is a great way to find something to contribute on. As our projects, by default, use the default GitHub issue labels (enhancement/bug/duplicate/help wanted/invalid/question/wontfix), looking at any 'help wanted' issues is a great place to start.


## Code of Conduct
This project has adopted the [Amazon Open Source Code of Conduct](https://aws.github.io/code-of-conduct).
For more information see the [Code of Conduct FAQ](https://aws.github.io/code-of-conduct-faq) or contact
opensource-codeofconduct@amazon.com with any additional questions or comments.


## Security issue notifications
If you discover a potential security issue in this project we ask that you notify AWS/Amazon Security via our [vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/). Please do **not** create a public github issue.


## Licensing

See the [LICENSE](LICENSE) file for our project's licensing. We will ask you to confirm the licensing of your contribution.
