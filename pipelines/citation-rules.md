# How to cite in this repo

The rules that make a citation checkable, extracted from `pipelines/contracts.md` so a reader
looking for a contract is not stopped by citation doctrine first. The cite auditor that enforces
them is `scripts/audit_doc_cites.py`.

Reading citations here: the **symbol** is the anchor (`store_ld8.run_ld8`, `_checked_body`,
`PAIR_WINDOWS`), never a number — a line number is stated only with an `as of <sha>`, since a bare
line number breaks silently at the next commit. Cite what you read, at the revision you read it
(`git show <sha>:<path>`), and end the citation where the *statement* ends, not where your reading
did. A **constant offset** between two readings means two revisions, not an error: `git show` both
before publishing a correction. A pin is **per cited file, not repo HEAD** (`git log -1 --format=%h --
<path>`) and is **regenerated at paste time, never copied**, since a copied value rots before the doc
stores it.

Citations also name a **tree**: `db/**` and `services/api/**` resolve to **app's** checkout,
`pipelines/**` to **data's** (see the mirror-boundary section at the end of this file). A path
mirrored byte-identically in two trees cannot carry a pin — **currency is the tree's property,
content is the revision's** — and a bare basename is a coin flip. Resolve by tree, and name the tree
when the number matters.

**Two genres, and the difference is what a sha can be checked against.** A path token (bare or
backticked; backticks are punctuation, not the rule) immediately followed by a backticked sha **on the
same line** is a **pin** — equality with that file's last-changed revision. A sha carrying a cue word
(`read at` / `measured at` / `as of`) is a **label** — existence plus ancestry, and with a path, that
the path was there at that sha. A label proves **never** content: a count-neutral edit leaves every
line valid while replacing the words at them, so only a human re-verifies a quotation. A default run
is **markdown only** and `scripts/audit_doc_cites.py` prints that boundary beside its counts; a sha
with neither a path nor a repo name is unjudgeable, reported as `unclassified`, never guessed. A row
id is not a revision, and one pin never covers several paths or a wrapped pair.

A count, by contrast, is a claim about the invocation that produced it, and the guard prints its
harness — coverage mode, peer set, tree — beside every bucket. Publish the line, not the number: one
unchanged tree reports a different `ok` in markdown-only and `--comments` mode, and `--peer`
**replaces** the defaults, so declaring a single peer turns every other tree's cites into findings
*about the docs* rather than about the invocation. The same trap once covered the peer set, which
appeared nowhere in the output, so the invocation had to travel with the line for `cite-blank` and
`cite-tree` to be re-derivable at all; every printed line now names its peers (`d61978b`, `ef94a56`),
and that half of the rule is **retired by the line itself** rather than demanded of authors -- prefer
the mechanism over the discipline wherever the mechanism can be built. A count from a superseded
invocation is
**withdrawn, never diffed** against a fresh one, and the withdrawal is a property of the pair: name
the set you are superseding in the same message, restating that one hop and marking anything older as
superseded rather than restating it -- the earlier message cannot be edited, and a chain of
live-looking sets is the very failure this rule exists to prevent.

Every zero is one of three different claims, and they are not interchangeable: a **mechanical green**
(the tool decided it), a **peer-verified green** (someone read the span at a named revision — the only
form available where there is no symbol to name), and a **checked skip** (the bucket declined
knowably: `cite-symbol-unchecked`, `rowid-skipped`). A report that shows only "0 findings" flattens
exactly the third, which is the one that carries the information for a span with no symbol in it.

A grep-unique statement name is the primary cite; a bare `:NN` is legitimate only where the file is
named in the same sentence, and a bare number with **no referent in scope** clears the checks
vacuously. **"Your file still says X" is itself a claim that needs its revision**: quote it with
`git show <sha>:<path>`, and read `git log --oneline -- <path>` to see whether a later commit already
acted on it. Facts from the shared agent daily log carry **no provenance** unless prefixed
(`app:`/`data:`/`ml:`); re-derive before citing.

A **docstring-only commit** is still a claim: `git show --stat`, then parse both revisions, blank every
leading string constant (`ast.Constant` at `body[0]`) and compare `ast.dump` — equality means no code
moved, but only where the diff is inside existing string constants, because an added `ast.keyword` is
structure blanking cannot remove; a changed call signature needs the hunk read. The **subject** is a
claim too: a `docs(...)` subject licenses "no logic moved", never "nothing user-visible changed",
because `--help` text *is* user-visible behaviour. A **number inside a message is a claim like any
other**: run the command **at the revision you cite** and paste its output, using `--numstat`
(`git show --numstat <sha> -- <path>`, per-file, tab-separated), not `--stat`, since a multi-file
commit's per-file pair cannot be summed as one `+N/-M`. A figure is checkable only when the same
sentence binds it to a sha; a number written with the commit, before the sha exists, can only be
re-derived afterwards.
