// Copyright (C) 2026 The Android Open Source Project
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
// http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package com.googlesource.gerrit.plugins.githubarchiver;

import com.google.gerrit.extensions.annotations.PluginName;
import com.google.gerrit.extensions.api.GerritApi;
import com.google.gerrit.extensions.client.ListChangesOption;
import com.google.gerrit.extensions.common.ChangeInfo;
import com.google.gerrit.extensions.common.CommentInfo;
import com.google.gerrit.extensions.events.LifecycleListener;
import com.google.gerrit.server.config.ScheduleConfig;
import com.google.gerrit.server.git.GitRepositoryManager;
import com.google.gerrit.server.git.WorkQueue;
import com.google.gerrit.server.util.ManualRequestContext;
import com.google.gerrit.server.util.OneOffRequestContext;
import com.googlesource.gerrit.plugins.githubarchiver.ArchiverConfig.ProjectMapping;
import com.google.inject.Inject;
import com.google.inject.Singleton;
import java.io.IOException;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.EnumSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.logging.Level;
import java.util.logging.Logger;

/**
 * Owns the ledger, the work queue and the scheduled sweep.
 *
 * <p>The sweep is the correctness mechanism, not the listeners. Events can be missed — a plugin
 * reload, a projection that threw, a queue that shed load under a burst — so nothing may depend on
 * an event having been delivered. The sweep re-derives the desired state from Gerrit and converges
 * the GitHub side, and the listeners only make it happen sooner.
 *
 * <p>Listeners never call into this synchronously beyond {@link #enqueue}: blocking a Gerrit
 * listener thread on a GitHub round trip would stall the review server, which is the main hazard of
 * running in-process at all.
 */
@Singleton
public class ArchiverService implements LifecycleListener {

  private static final Logger log = Logger.getLogger(ArchiverService.class.getName());

  private static final EnumSet<ListChangesOption> CHANGE_OPTIONS =
      EnumSet.of(
          ListChangesOption.ALL_REVISIONS,
          ListChangesOption.DETAILED_ACCOUNTS,
          ListChangesOption.MESSAGES,
          ListChangesOption.CURRENT_REVISION);

  private static final DateTimeFormatter QUERY_TIME =
      DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss").withZone(ZoneOffset.UTC);

  private final String pluginName;
  private final ArchiverConfig config;
  private final GerritApi gerritApi;
  private final GitRepositoryManager repoManager;
  private final OneOffRequestContext requestContext;
  private final WorkQueue workQueue;

  private final ArchiveQueue queue = new ArchiveQueue(1000);
  private final Map<String, Projector> projectors = new ConcurrentHashMap<>();

  private volatile Ledger ledger;
  private ScheduledExecutorService executor;
  private volatile boolean running;

  @Inject
  ArchiverService(
      @PluginName String pluginName,
      ArchiverConfig config,
      GerritApi gerritApi,
      GitRepositoryManager repoManager,
      OneOffRequestContext requestContext,
      WorkQueue workQueue) {
    this.pluginName = pluginName;
    this.config = config;
    this.gerritApi = gerritApi;
    this.repoManager = repoManager;
    this.requestContext = requestContext;
    this.workQueue = workQueue;
  }

  // -- lifecycle -------------------------------------------------------

  @Override
  public void start() {
    Optional<String> token = config.githubToken();
    if (token.isEmpty()) {
      log.severe(
          () ->
              "no GitHub token configured; set [plugin \""
                  + pluginName
                  + "\"] githubToken in secure.config. The archiver is idle.");
      return;
    }
    if (config.projects().isEmpty()) {
      log.warning(() -> "no [project] sections in " + pluginName + ".config; the archiver is idle");
      return;
    }
    for (String bad : config.incompleteProjects()) {
      log.warning(() -> "project \"" + bad + "\" is missing owner or repo; skipping it");
    }

    try {
      ledger = new Ledger(config.dataDir().resolve("ledger.log"));
    } catch (IOException e) {
      log.log(Level.SEVERE, "cannot open the ledger; the archiver is idle", e);
      return;
    }

    running = true;
    // Two workers is plenty: work is serialised per change anyway, and more
    // concurrency mostly buys a faster way to hit GitHub's secondary limit.
    executor = workQueue.createQueue(2, pluginName);
    executor.execute(this::workerLoop);
    executor.execute(this::workerLoop);

    Optional<ScheduleConfig.Schedule> schedule =
        ScheduleConfig.createSchedule(config.rawConfig(), "sweep");
    if (schedule.isPresent()) {
      executor.scheduleAtFixedRate(
          this::sweepQuietly,
          schedule.get().initialDelay(),
          schedule.get().interval(),
          TimeUnit.MILLISECONDS);
      log.info(
          () ->
              "sweep scheduled every "
                  + Duration.ofMillis(schedule.get().interval()).toMinutes()
                  + " minutes");
    } else {
      // Without a schedule the listeners are all there is, and a missed event
      // is then lost for good. Say so rather than appear to be working.
      log.warning(
          () ->
              "no [sweep] interval configured; running on events only. A missed event will"
                  + " not be recovered. Set sweep.interval to restore the guarantee.");
    }
    log.info(() -> pluginName + " started for projects " + config.projects().keySet());
  }

  @Override
  public void stop() {
    running = false;
    queue.close();
    if (executor != null) {
      executor.shutdownNow();
    }
    if (ledger != null) {
      try {
        ledger.close();
      } catch (IOException e) {
        log.log(Level.WARNING, "error closing the ledger", e);
      }
    }
  }

  public boolean isRunning() {
    return running;
  }

  // -- enqueueing ------------------------------------------------------

  /** Asks for a change to be projected soon. Safe to call from a listener thread. */
  public void enqueue(String project, int changeNumber) {
    if (!running) {
      return;
    }
    if (!config.projects().containsKey(project)) {
      return;
    }
    queue.put(new ArchiveQueue.Target(project, changeNumber));
  }

  private void workerLoop() {
    while (running) {
      ArchiveQueue.Target target;
      try {
        Optional<ArchiveQueue.Target> next = queue.take();
        if (next.isEmpty()) {
          return;
        }
        target = next.get();
      } catch (InterruptedException e) {
        Thread.currentThread().interrupt();
        return;
      }
      try {
        projectOne(target.project(), target.changeNumber());
      } catch (Exception e) {
        // One bad change must not kill the worker.
        log.log(Level.WARNING, "projection failed for " + target.key(), e);
      } finally {
        queue.done(target);
      }
    }
  }

  // -- projection ------------------------------------------------------

  private Projector projectorFor(ProjectMapping mapping, String token) {
    return projectors.computeIfAbsent(
        mapping.gerritProject,
        name ->
            new Projector(
                config,
                mapping,
                new GitHubClient(config.githubApiUrl(), token, mapping.owner, mapping.repo),
                new JGitOps(
                    repoManager, "https://github.com/" + mapping.slug() + ".git", token),
                ledger));
  }

  /**
   * Projects one change.
   *
   * <p>Reads are done in a request context as the internal user, which is what lets a background
   * thread use the Gerrit API at all. That user sees everything, including private changes, which
   * is exactly why the projector refuses them explicitly rather than relying on visibility.
   */
  public Optional<Projector.Result> projectOne(String project, int changeNumber) throws Exception {
    ProjectMapping mapping = config.projects().get(project);
    if (mapping == null) {
      return Optional.empty();
    }
    Optional<String> token = config.githubToken();
    if (token.isEmpty()) {
      return Optional.empty();
    }

    ChangeInfo change;
    Map<String, List<CommentInfo>> comments;
    try (ManualRequestContext ctx = requestContext.open()) {
      var api = gerritApi.changes().id(project, changeNumber);
      change = api.get(CHANGE_OPTIONS);
      // withContext is the typed enable-context: it is what populates
      // contextLines, which the issue-comment fallback quotes. Omitting the
      // equivalent parameter was a real bug in the external implementation.
      comments = api.commentsRequest().withContext(true).get();
    }

    Projector.Result result = projectorFor(mapping, token.get()).project(change, comments);
    if (result.reviewsPosted > 0 || result.createdPr) {
      log.info(
          () ->
              "["
                  + result.changeKey
                  + "] PR #"
                  + result.prNumber
                  + ": "
                  + result.reviewsPosted
                  + " reviews, "
                  + result.commentsPosted
                  + " inline, "
                  + result.fallbacksPosted
                  + " fallback"
                  + (result.createdPr ? " (created)" : ""));
    }
    return Optional.of(result);
  }

  // -- sweep -----------------------------------------------------------

  /** Aggregate outcome of one sweep, for logging and the ssh command. */
  public static class SweepStats {
    public int inspected;
    public int projected;
    public int skipped;
    public int failed;
    public final List<String> errors = new ArrayList<>();

    @Override
    public String toString() {
      return "inspected="
          + inspected
          + " projected="
          + projected
          + " skipped="
          + skipped
          + " failed="
          + failed;
    }
  }

  private void sweepQuietly() {
    try {
      SweepStats stats = sweep(false);
      log.info(() -> "sweep done: " + stats);
    } catch (Exception e) {
      // The scheduled task must outlive any single sweep.
      log.log(Level.WARNING, "sweep failed", e);
    }
  }

  /**
   * Re-derives state from Gerrit and converges GitHub.
   *
   * @param full ignore the lookback window and inspect every change
   */
  public SweepStats sweep(boolean full) throws Exception {
    SweepStats stats = new SweepStats();
    if (!running) {
      return stats;
    }
    for (ProjectMapping mapping : config.projects().values()) {
      StringBuilder q = new StringBuilder("project:").append(mapping.gerritProject);
      // Private changes are excluded here as well as in the projector, so a
      // visibility mistake needs two independent failures.
      q.append(" -is:private");
      if (!full) {
        Instant since =
            Instant.now().minus(Duration.ofMinutes(config.lookbackMinutes()));
        q.append(" after:\"").append(QUERY_TIME.format(since)).append('"');
      }

      List<ChangeInfo> changes;
      try (ManualRequestContext ctx = requestContext.open()) {
        changes =
            gerritApi
                .changes()
                .query(q.toString())
                .withLimit(config.sweepLimit())
                .withOptions(CHANGE_OPTIONS)
                .get();
      }
      log.info(() -> "[" + mapping.gerritProject + "] sweep query: " + q);

      for (ChangeInfo change : changes) {
        stats.inspected++;
        String key = Ledger.changeKey(mapping.gerritProject, change._number);
        String updated = change.updated == null ? null : change.updated.toString();
        Optional<Ledger.ChangeRecord> record = ledger.getChange(key);
        if (!full
            && record.isPresent()
            && updated != null
            && updated.equals(record.get().lastUpdated)) {
          stats.skipped++;
          continue;
        }
        try {
          Optional<Projector.Result> r = projectOne(mapping.gerritProject, change._number);
          if (r.isPresent() && r.get().skippedReason != null) {
            stats.skipped++;
          } else if (r.isPresent()) {
            stats.projected++;
          }
        } catch (Exception e) {
          stats.failed++;
          stats.errors.add(key + ": " + e);
          log.log(Level.WARNING, "sweep projection failed for " + key, e);
        }
      }
    }
    return stats;
  }
}
