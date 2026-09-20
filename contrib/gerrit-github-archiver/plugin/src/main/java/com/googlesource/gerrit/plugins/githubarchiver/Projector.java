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

import com.google.gerrit.extensions.client.ChangeStatus;
import com.google.gerrit.extensions.common.ChangeInfo;
import com.google.gerrit.extensions.common.CommentInfo;
import com.google.gerrit.extensions.common.RevisionInfo;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.googlesource.gerrit.plugins.githubarchiver.ArchiverConfig.ProjectMapping;
import java.io.IOException;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.logging.Level;
import java.util.logging.Logger;

/**
 * Projects one Gerrit change onto one GitHub pull request.
 *
 * <p>Every GitHub write is gated on the ledger, so calling {@link #project} repeatedly for the same
 * change converges instead of duplicating.
 *
 * <p>Running in-process removes the mirror the external version needed: patch set commits are
 * already in the Gerrit repository this plugin can open directly, so there is nothing to clone and
 * no fetch to keep current.
 */
public class Projector {

  private static final Logger log = Logger.getLogger(Projector.class.getName());

  /** Outcome of one projection, for logging and the ssh command's output. */
  public static class Result {
    public final String changeKey;
    public Integer prNumber;
    public boolean createdPr;
    public int reviewsPosted;
    public int commentsPosted;
    public int fallbacksPosted;
    public String skippedReason;
    /** Set when a human has to look at something the plugin cannot fix. */
    public boolean needsAttention;

    Result(String changeKey) {
      this.changeKey = changeKey;
    }
  }

  private final ArchiverConfig config;
  private final ProjectMapping mapping;
  private final GitHubClient github;
  private final GitOps git;
  private final Ledger ledger;

  public Projector(
      ArchiverConfig config,
      ProjectMapping mapping,
      GitHubClient github,
      GitOps git,
      Ledger ledger) {
    this.config = config;
    this.mapping = mapping;
    this.github = github;
    this.git = git;
    this.ledger = ledger;
  }

  private String titleFor(ChangeInfo change) {
    String subject = change.subject == null ? "(no subject)" : change.subject;
    return subject + " (Gerrit " + change._number + ")";
  }

  /** SHA of the highest-numbered patch set. */
  static Optional<Map.Entry<String, RevisionInfo>> currentRevision(ChangeInfo change) {
    if (change.revisions == null || change.revisions.isEmpty()) {
      return Optional.empty();
    }
    Map.Entry<String, RevisionInfo> best = null;
    for (Map.Entry<String, RevisionInfo> e : change.revisions.entrySet()) {
      if (best == null || e.getValue()._number > best.getValue()._number) {
        best = e;
      }
    }
    return Optional.ofNullable(best);
  }

  // -- git -------------------------------------------------------------

  private String ensureHeadPushed(String key, ChangeInfo change, String branch) throws IOException {
    Map.Entry<String, RevisionInfo> current =
        currentRevision(change).orElseThrow(() -> new IOException("change has no revision"));
    String sha = current.getKey();
    int number = current.getValue()._number;
    if (ledger.pushedSha(key, number).map(sha::equals).orElse(false)) {
      return sha;
    }
    git.pushArchiveHead(change.project, sha, branch);
    ledger.recordPatchSet(key, number, sha);
    return sha;
  }

  // -- pull request lifecycle ------------------------------------------

  private JsonObject createPr(ChangeInfo change, String branch, String sha, boolean draft)
      throws IOException {
    String body = Projection.renderPrBody(change, config.canonicalWebUrl());
    String base = change.branch == null ? "master" : change.branch;
    try {
      return github.createPr(titleFor(change), branch, base, body, draft);
    } catch (GitHubClient.UnprocessableEntity e) {
      if (!e.getMessage().contains("No commits between")) {
        throw e;
      }
      // The change already landed and was mirrored, so head is an ancestor of
      // base and GitHub sees an empty diff. Pin a synthetic base at the
      // commit's parent so the diff still renders. The pull request then
      // closes rather than showing a Merged badge, which the body records.
      String parent = git.parentOf(change.project, sha).orElseThrow(() -> e);
      String baseBranch = branch + "-base";
      git.pushArchiveHead(change.project, parent, baseBranch);
      log.info(() -> "change already merged upstream; pinning synthetic base " + baseBranch);
      return github.createPr(
          titleFor(change),
          branch,
          baseBranch,
          body
              + "\n\n_Archived after the change had already merged, so the base is pinned to the"
              + " parent commit and GitHub shows this as closed rather than merged._",
          draft);
    }
  }

  private JsonObject ensurePr(String key, ChangeInfo change, String branch, String sha, Result r)
      throws IOException {
    Optional<Ledger.ChangeRecord> record = ledger.getChange(key);
    if (record.isPresent() && record.get().prNumber != null) {
      return github.getPr(record.get().prNumber);
    }
    Optional<JsonObject> existing = github.findPrByHead(branch);
    if (existing.isPresent()) {
      log.info(() -> "adopting existing PR #" + existing.get().get("number") + " for " + key);
      return existing.get();
    }
    boolean draft = Boolean.TRUE.equals(change.workInProgress);
    JsonObject pr = createPr(change, branch, sha, draft);
    r.createdPr = true;
    log.info(() -> "created PR #" + pr.get("number") + " for " + key);
    return pr;
  }

  /**
   * Re-imports already-posted GitHub items into the ledger.
   *
   * <p>Closes the crash window between a successful GitHub write and the matching ledger write:
   * anything carrying the provenance marker is recognised as projected, so it is never posted
   * twice.
   */
  private int adoptExisting(String key, int prNumber) throws IOException {
    int adopted = 0;
    List<JsonObject> all = new ArrayList<>();
    all.addAll(github.listIssueComments(prNumber));
    all.addAll(github.listReviewComments(prNumber));
    all.addAll(github.listReviews(prNumber));
    for (JsonObject item : all) {
      String body = item.has("body") && !item.get("body").isJsonNull()
          ? item.get("body").getAsString()
          : null;
      String[] parsed = Projection.parseMarker(body);
      if (parsed != null
          && ledger.recordSynced(key, parsed[0], parsed[1], idOf(item))) {
        adopted++;
      }
    }
    if (adopted > 0) {
      int count = adopted;
      log.info(() -> "adopted " + count + " pre-existing GitHub items for " + key);
    }
    return adopted;
  }

  private static String idOf(JsonObject o) {
    return o.has("id") && !o.get("id").isJsonNull() ? o.get("id").getAsString() : null;
  }

  private void syncPrState(ChangeInfo change, JsonObject pr) throws IOException {
    int number = pr.get("number").getAsInt();
    JsonObject fields = new JsonObject();

    String desiredTitle = titleFor(change);
    String currentTitle =
        pr.has("title") && !pr.get("title").isJsonNull() ? pr.get("title").getAsString() : "";
    if (!desiredTitle.equals(currentTitle)) {
      fields.addProperty("title", desiredTitle);
    }

    String state =
        pr.has("state") && !pr.get("state").isJsonNull() ? pr.get("state").getAsString() : "open";
    // A merged change is closed by GitHub itself once replication advances the
    // base branch; closing it here would show Closed rather than Merged.
    if (change.status == ChangeStatus.ABANDONED && "open".equals(state)) {
      fields.addProperty("state", "closed");
    } else if (change.status == ChangeStatus.NEW && "closed".equals(state)) {
      fields.addProperty("state", "open");
    }

    if (!fields.isEmpty()) {
      github.updatePr(number, fields);
    }

    boolean isDraft = pr.has("draft") && !pr.get("draft").isJsonNull() && pr.get("draft").getAsBoolean();
    if (isDraft && !Boolean.TRUE.equals(change.workInProgress)) {
      if (pr.has("node_id") && !pr.get("node_id").isJsonNull()) {
        github.markReadyForReview(pr.get("node_id").getAsString());
      }
    }
  }

  // -- review projection -----------------------------------------------

  private int postFallbacks(String key, int prNumber, List<CommentInfo> comments)
      throws IOException {
    int posted = 0;
    for (CommentInfo c : comments) {
      if (c.id == null || ledger.isSynced(key, Ledger.KIND_COMMENT, c.id)) {
        continue;
      }
      JsonObject created = github.createIssueComment(prNumber, Projection.renderFallbackBody(c));
      ledger.recordSynced(key, Ledger.KIND_COMMENT, c.id, idOf(created));
      posted++;
    }
    return posted;
  }

  private static JsonObject inlinePayload(Projection.InlineComment c, String headSha) {
    JsonObject o = new JsonObject();
    o.addProperty("path", c.path());
    o.addProperty("body", c.body());
    o.addProperty("line", c.anchor().line());
    o.addProperty("side", c.anchor().side());
    if (c.anchor().startLine() != null) {
      o.addProperty("start_line", c.anchor().startLine());
      o.addProperty("start_side", c.anchor().startSide());
    }
    if (headSha != null) {
      o.addProperty("commit_id", headSha);
    }
    return o;
  }

  /**
   * Posts inline comments one at a time, collecting the unanchorable ones.
   *
   * <p>Used both when GitHub rejects a batched review and when resuming a review whose body already
   * posted on an earlier pass.
   */
  private int postCommentsIndividually(
      String key,
      int prNumber,
      List<Projection.InlineComment> inline,
      String headSha,
      List<CommentInfo> stranded)
      throws IOException {
    int posted = 0;
    for (Projection.InlineComment c : inline) {
      String gerritId = c.source().id;
      if (gerritId != null && ledger.isSynced(key, Ledger.KIND_COMMENT, gerritId)) {
        continue;
      }
      try {
        JsonObject created =
            github.createReviewComment(prNumber, inlinePayload(c, headSha));
        if (gerritId != null) {
          ledger.recordSynced(key, Ledger.KIND_COMMENT, gerritId, idOf(created));
        }
        posted++;
      } catch (GitHubClient.UnprocessableEntity e) {
        // Not anchorable at all; degrade to a quoted issue comment.
        stranded.add(c.source());
      }
    }
    return posted;
  }

  /**
   * Posts one Gerrit review as one GitHub review.
   *
   * <p>On 422 the batch is retried per comment, because GitHub rejects the whole review when any
   * single comment falls outside a diff hunk and does not say which one.
   */
  private void postReview(
      String key, int prNumber, Projection.ReviewPlan plan, String headSha, Result r)
      throws IOException {
    List<CommentInfo> fallbacks = new ArrayList<>(plan.fallback);

    if (plan.bodyAlreadyPosted) {
      // The body reached GitHub earlier; only comments remain, so do not
      // create a second review.
      r.commentsPosted +=
          postCommentsIndividually(key, prNumber, plan.inline, headSha, fallbacks);
    } else {
      JsonArray batch = new JsonArray();
      for (Projection.InlineComment c : plan.inline) {
        // commit_id is not accepted inside a batched review.
        batch.add(inlinePayload(c, null));
      }
      try {
        JsonObject review = github.createReview(prNumber, plan.body, batch);
        ledger.recordSynced(key, Ledger.KIND_MESSAGE, plan.messageId, idOf(review));
        for (Projection.InlineComment c : plan.inline) {
          if (c.source().id != null) {
            ledger.recordSynced(key, Ledger.KIND_COMMENT, c.source().id, null);
          }
        }
        r.commentsPosted += plan.inline.size();
      } catch (GitHubClient.UnprocessableEntity e) {
        log.warning(
            () ->
                "batched review rejected for "
                    + key
                    + "; retrying comment by comment: "
                    + e.getMessage());
        JsonObject review = github.createReview(prNumber, plan.body, new JsonArray());
        ledger.recordSynced(key, Ledger.KIND_MESSAGE, plan.messageId, idOf(review));
        r.commentsPosted +=
            postCommentsIndividually(key, prNumber, plan.inline, headSha, fallbacks);
      }
      r.reviewsPosted++;
    }

    r.fallbacksPosted += postFallbacks(key, prNumber, fallbacks);
  }

  // -- entry point -----------------------------------------------------

  /** Converges the GitHub side for one change. */
  public Result project(ChangeInfo change, Map<String, List<CommentInfo>> comments)
      throws IOException {
    String projectName = change.project == null ? mapping.gerritProject : change.project;
    int number = change._number;
    String key = Ledger.changeKey(projectName, number);
    Result r = new Result(key);

    if (config.skipPrivate() && Boolean.TRUE.equals(change.isPrivate)) {
      r.skippedReason = "private";
      Optional<Ledger.ChangeRecord> record = ledger.getChange(key);
      if (record.isPresent() && record.get().prNumber != null) {
        // Already published before it was made private. Nothing the API can
        // do undoes that, so make it loud rather than silent.
        log.log(
            Level.SEVERE,
            "change {0} became private but is already archived as {1}#{2}; review manually",
            new Object[] {key, mapping.slug(), record.get().prNumber});
        r.needsAttention = true;
      }
      return r;
    }

    if (!projectName.equals(mapping.gerritProject)) {
      // Defence in depth: never push one project's change into another's repo.
      log.warning(
          () -> "refusing to project " + key + " under the " + mapping.gerritProject + " mapping");
      r.skippedReason = "project " + projectName + " not mapped here";
      return r;
    }

    if (!mapping.archivesBranch(change.branch)) {
      r.skippedReason = "branch " + change.branch + " not archived";
      return r;
    }

    String branch = mapping.branchFor(number);
    ledger.upsertChange(key, null, branch, null, String.valueOf(change.status));

    String sha = ensureHeadPushed(key, change, branch);
    JsonObject pr = ensurePr(key, change, branch, sha, r);
    int prNumber = pr.get("number").getAsInt();
    r.prNumber = prNumber;
    ledger.upsertChange(key, prNumber, branch, null, null);

    if (!r.createdPr) {
      adoptExisting(key, prNumber);
    }
    syncPrState(change, pr);

    String headSha =
        pr.has("head") && pr.getAsJsonObject("head").has("sha")
            ? pr.getAsJsonObject("head").get("sha").getAsString()
            : sha;

    Set<String> syncedMessages = ledger.syncedIds(key, Ledger.KIND_MESSAGE);
    Set<String> syncedComments = ledger.syncedIds(key, Ledger.KIND_COMMENT);
    List<Projection.ReviewPlan> plans =
        Projection.planReviews(
            change, comments, syncedMessages, syncedComments, config.skipAutogenerated());

    for (Projection.ReviewPlan plan : plans) {
      postReview(key, prNumber, plan, headSha, r);
    }

    ledger.upsertChange(
        key,
        null,
        null,
        change.updated == null ? null : change.updated.toString(),
        String.valueOf(change.status));
    return r;
  }
}
