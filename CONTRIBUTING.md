# Contributing

Thanks for your interest in contributing! This repository is a fork of the
Android `repo` tool with an optional AI assistant layer (`repoai/`). We accept
contributions via **GitHub pull requests**.

## Before you start

* Make small, logical, separately reviewable changes.
* Write a meaningful commit message
  (see [commit message style][commit-message-style]).
* All code must be under the **Apache License, 2.0**. Every file must carry the
  Apache license header.

## Sign your work (DCO)

We use the [Developer Certificate of Origin][dco]. Add a `Signed-off-by` line to
each commit:

```sh
git commit -s -m "summary of change"
```

This certifies that you wrote the patch or otherwise have the right to pass it
on under the Apache 2.0 license.

## License headers

Because of the Apache 2.0 model, **every** file must list its license in the
header. New files must clearly state they are provided under the Apache License,
2.0. New contributions to an existing file must be submitted under that file's
current license. Example header:

```python
# Copyright (C) 2026 <Your Name or Organization>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
```

## Linting and formatting

```sh
$ flake8
$ black file.py        # line length 80
```

`repo` follows [Google's Python Style Guide][google-style]. No new errors or
warnings should be introduced.

## Running tests

We use [pytest][pytest]. `./run_tests` is a thin wrapper:

```sh
$ ./run_tests                 # full suite, default Python
$ ./run_tests -v              # verbose
$ ./run_tests tests/test_repoai.py
$ ./run_tests -k test_cat_editor
```

Please run the suite (at least `tests/test_repoai.py` for AI-layer changes)
before opening a PR.

## Opening a pull request

1. Fork the repo and push your branch.
2. Open a PR against `main` with a clear description and the `Signed-off-by`
   trailer on every commit.
3. Make requested changes by amending/adding commits; we review on GitHub.

[commit-message-style]: https://chris.beams.io/posts/git-commit/
[dco]: https://developercertificate.org/
[google-style]: https://google.github.io/styleguide/pyguide.html
[pytest]: https://pytest.org/
