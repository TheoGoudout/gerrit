Archives Gerrit reviews onto GitHub pull requests, so the review record
survives a migration away from Gerrit.

Each Gerrit change becomes one pull request in a mapped GitHub repository.
Each Gerrit review becomes one GitHub review, with inline comments anchored
where they were written and attribution written into the comment body rather
than relying on GitHub actorship, which degrades to `ghost` when an account is
deleted.

This is the readable layer only. The durable record is the `replication`
plugin mirroring `refs/changes/*` and `All-Users` to a private archive
repository, which loses nothing; this plugin accepts the fidelity limits of
GitHub's model in exchange for something people can browse.

Correctness comes from the scheduled sweep, not from the event listeners. A
listener can be missed — a reload, a projection that threw, a queue shedding
load under a burst — so the sweep re-derives state from Gerrit and converges
the GitHub side. Configure `sweep.interval`; without it the plugin runs on
events alone and a missed event is lost for good.

See `config.md` for configuration, and the `sweep` ssh command for running a
reconciliation on demand.
