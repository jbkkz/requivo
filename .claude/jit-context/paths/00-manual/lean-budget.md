---
title: "Ceilings only go down"
description: "Lowering a ceiling is a one-line PR; raising one costs a sentence saying why the tree grew (#553)."
match: ^tests/lean_budget\.toml$
---

Every value here is a ceiling measured on the real tree, rounded up by at most 5%, and read by
`test_the_tree_stays_within_its_lean_budget`. A change that gets under one should lower it. Raising
one is legal and visible, and costs the PR one sentence in this file saying why the tree needed to
grow. No key is a target, and none belongs in prose anywhere else.
