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

import com.google.gerrit.extensions.client.Side;
import com.google.gerrit.extensions.common.AccountInfo;
import com.google.gerrit.extensions.common.ChangeInfo;
import com.google.gerrit.extensions.common.ChangeMessageInfo;
import com.google.gerrit.extensions.common.CommentInfo;
import com.google.gerrit.extensions.common.ContextLineInfo;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Pure mapping from Gerrit review data onto GitHub payloads.
 *
 * <p>No I/O, so the fiddly parts — comment anchoring, review grouping, attribution — are testable
 * on their own.
 */
public final class Projection {

  /** Gerrit pseudo-files that have no position in a GitHub diff. */
  public static final String COMMIT_MSG = "/COMMIT_MSG";
  public static final String MERGE_LIST = "/MERGE_LIST";
  public static final String PATCHSET_LEVEL = "/PATCHSET_LEVEL";
  public static final Set<String> PSEUDO_FILES = Set.of(COMMIT_MSG, MERGE_LIST, PATCHSET_LEVEL);

  private static final Pattern MARKER =
      Pattern.compile("<!--\\s*gga:([a-z_]+):([^\\s>]+)\\s*-->");
  private static final DateTimeFormatter STAMP =
      DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm 'UTC'").withZone(ZoneOffset.UTC);

  private Projection() {}

  /** Where a comment attaches in a GitHub diff. */
  public record Anchor(int line, String side, Integer startLine, String startSide) {}

  /** One Gerrit review action, ready to post. */
  public static final class ReviewPlan {
    public final String messageId;
    public final String body;
    /** True when the body posted on an earlier pass and only comments remain. */
    public final boolean bodyAlreadyPosted;

    public final List<InlineComment> inline = new ArrayList<>();
    public final List<CommentInfo> fallback = new ArrayList<>();
    public final List<CommentInfo> sourceComments = new ArrayList<>();

    ReviewPlan(String messageId, String body, boolean bodyAlreadyPosted) {
      this.messageId = messageId;
      this.body = body;
      this.bodyAlreadyPosted = bodyAlreadyPosted;
    }

    public List<String> commentIds() {
      return sourceComments.stream().map(c -> c.id).toList();
    }

    public boolean isEmpty() {
      return body.isBlank() && inline.isEmpty() && fallback.isEmpty();
    }
  }

  /** A GitHub review comment payload plus the Gerrit comment it came from. */
  public record InlineComment(String path, String body, Anchor anchor, CommentInfo source) {}

  // -- markers ---------------------------------------------------------

  public static String marker(String kind, String gerritId) {
    return "<!-- gga:" + kind + ":" + gerritId + " -->";
  }

  /** Returns {@code [kind, gerritId]}, or null when the body carries no marker. */
  public static String[] parseMarker(String body) {
    if (body == null || body.isEmpty()) {
      return null;
    }
    Matcher m = MARKER.matcher(body);
    return m.find() ? new String[] {m.group(1), m.group(2)} : null;
  }

  // -- rendering -------------------------------------------------------

  public static String formatTimestamp(java.sql.Timestamp ts) {
    return ts == null ? "unknown time" : STAMP.format(ts.toInstant());
  }

  /**
   * Renders an account as durable text.
   *
   * <p>Deliberately textual: GitHub actorship degrades to {@code ghost} when an account is deleted,
   * whereas a name in the body survives.
   */
  public static String formatAuthor(AccountInfo account) {
    if (account == null) {
      return "Unknown";
    }
    String name = account.name != null ? account.name : account.username;
    if (name == null) {
      name = "Unknown";
    }
    return account.email != null ? name + " <" + account.email + ">" : name;
  }

  public static String attribution(AccountInfo author, java.sql.Timestamp when, Integer patchSet) {
    StringBuilder sb = new StringBuilder();
    sb.append("**").append(formatAuthor(author)).append("**");
    sb.append(" · ").append(formatTimestamp(when));
    if (patchSet != null) {
      sb.append(" · Patch Set ").append(patchSet);
    }
    return sb.toString();
  }

  public static String githubSide(CommentInfo c) {
    return c.side == Side.PARENT ? "LEFT" : "RIGHT";
  }

  /**
   * Returns the GitHub line anchor, or null when the comment cannot be inline.
   *
   * <p>GitHub rejects review comments outside a diff hunk with 422, and has no position at all for
   * Gerrit's pseudo-files or for file-level comments, so those go to the fallback path.
   */
  public static Anchor anchorFor(CommentInfo c) {
    if (c.path == null || PSEUDO_FILES.contains(c.path)) {
      return null;
    }
    String side = githubSide(c);
    if (c.range != null) {
      int end = c.range.endLine;
      if (end <= 0) {
        return null;
      }
      int start = c.range.startLine;
      // GitHub rejects start_line equal to line, so only emit a real span.
      return start > 0 && start < end
          ? new Anchor(end, side, start, side)
          : new Anchor(end, side, null, null);
    }
    if (c.line != null && c.line > 0) {
      return new Anchor(c.line, side, null, null);
    }
    // No line and no range is a file-level comment in Gerrit.
    return null;
  }

  public static String renderCommentBody(CommentInfo c) {
    StringBuilder sb = new StringBuilder();
    sb.append(attribution(c.author, c.updated, c.patchSet)).append('\n');
    sb.append(marker(Ledger.KIND_COMMENT, c.id)).append("\n\n");
    sb.append(c.message == null ? "" : c.message);
    if (Boolean.TRUE.equals(c.unresolved)) {
      sb.append("\n\n_Marked unresolved in Gerrit._");
    }
    return sb.toString().strip();
  }

  /**
   * Renders a comment that could not be anchored.
   *
   * <p>{@code contextLines} holds the source the reviewer was looking at, so the quote survives even
   * though the anchor does not. It is only populated when the comments were fetched with
   * enable-context.
   */
  public static String renderFallbackBody(CommentInfo c) {
    String path = c.path == null ? "(unknown file)" : c.path;
    String location;
    if (COMMIT_MSG.equals(path)) {
      location = "commit message";
    } else if (PATCHSET_LEVEL.equals(path)) {
      location = "patch set (no file)";
    } else if (MERGE_LIST.equals(path)) {
      location = "merge list";
    } else {
      Integer line = c.line != null ? c.line : (c.range != null ? c.range.endLine : null);
      location = line != null ? "`" + path + "` line " + line : "`" + path + "` (file-level)";
    }

    StringBuilder sb = new StringBuilder();
    sb.append(attribution(c.author, c.updated, c.patchSet)).append('\n');
    sb.append(marker(Ledger.KIND_COMMENT, c.id)).append("\n\n");
    sb.append("On ").append(location).append(":\n");
    if (c.contextLines != null && !c.contextLines.isEmpty()) {
      sb.append("\n```\n");
      for (ContextLineInfo ctx : c.contextLines) {
        sb.append(ctx.lineNumber).append('\t').append(ctx.contextLine == null ? "" : ctx.contextLine)
            .append('\n');
      }
      sb.append("```\n");
    }
    sb.append('\n').append(c.message == null ? "" : c.message);
    if (Boolean.TRUE.equals(c.unresolved)) {
      sb.append("\n\n_Marked unresolved in Gerrit._");
    }
    return sb.toString().strip();
  }

  public static String renderMessageBody(ChangeMessageInfo m) {
    return (attribution(m.author, m.date, m._revisionNumber)
            + "\n"
            + marker(Ledger.KIND_MESSAGE, m.id)
            + "\n\n"
            + (m.message == null ? "" : m.message))
        .strip();
  }

  public static String renderPrBody(ChangeInfo change, String canonicalWebUrl) {
    String base = canonicalWebUrl == null ? "" : canonicalWebUrl.replaceAll("/+$", "");
    StringBuilder sb = new StringBuilder();
    sb.append(
        "_Archived Gerrit review. This pull request is a read-only record; "
            + "the review happened in Gerrit._\n\n");
    sb.append("* **Gerrit change:** [")
        .append(change._number)
        .append("](")
        .append(base)
        .append("/c/")
        .append(change.project)
        .append("/+/")
        .append(change._number)
        .append(")\n");
    sb.append("* **Change-Id:** `").append(change.changeId).append("`\n");
    sb.append("* **Owner:** ").append(formatAuthor(change.owner)).append('\n');
    sb.append("* **Target branch:** `").append(change.branch).append("`\n");
    if (change.topic != null && !change.topic.isEmpty()) {
      sb.append("* **Topic:** `").append(change.topic).append("`\n");
    }
    sb.append("* **Created:** ").append(formatTimestamp(change.created));
    return sb.toString();
  }

  public static boolean isAutogenerated(ChangeMessageInfo m) {
    return m.tag != null && m.tag.startsWith("autogenerated:");
  }

  // -- grouping --------------------------------------------------------

  /**
   * Groups inline comments by the review action that published them.
   *
   * <p>Gerrit links each comment to its change message through {@code changeMessageId}, which is
   * what lets one Gerrit review become one GitHub review. Data predating that link falls back to a
   * synthetic key.
   */
  public static Map<String, List<CommentInfo>> groupCommentsByMessage(
      Map<String, List<CommentInfo>> commentsByPath) {
    Map<String, List<CommentInfo>> grouped = new LinkedHashMap<>();
    List<String> paths = new ArrayList<>(commentsByPath.keySet());
    paths.sort(Comparator.naturalOrder());
    for (String path : paths) {
      for (CommentInfo c : commentsByPath.get(path)) {
        if (c.path == null) {
          // The REST map keys by path and omits it from the value.
          c.path = path;
        }
        String key = c.changeMessageId;
        if (key == null || key.isEmpty()) {
          key =
              "synthetic:"
                  + c.patchSet
                  + ":"
                  + (c.author == null ? "?" : c.author._accountId)
                  + ":"
                  + c.updated;
        }
        grouped.computeIfAbsent(key, k -> new ArrayList<>()).add(c);
      }
    }
    for (List<CommentInfo> list : grouped.values()) {
      list.sort(
          Comparator.comparing((CommentInfo c) -> c.path == null ? "" : c.path)
              .thenComparing(c -> c.line == null ? 0 : c.line));
    }
    return grouped;
  }

  private static ReviewPlan fill(ReviewPlan plan, List<CommentInfo> comments) {
    for (CommentInfo c : comments) {
      plan.sourceComments.add(c);
      Anchor anchor = anchorFor(c);
      if (anchor == null) {
        plan.fallback.add(c);
      } else {
        plan.inline.add(new InlineComment(c.path, renderCommentBody(c), anchor, c));
      }
    }
    return plan;
  }

  /**
   * Computes the reviews still needing projection, oldest first.
   *
   * <p>Anything already in the ledger is filtered out here, which is what makes a re-run a no-op.
   * Message state and comment state are tracked separately so a review whose body posted but whose
   * comments did not is resumed rather than skipped.
   */
  public static List<ReviewPlan> planReviews(
      ChangeInfo change,
      Map<String, List<CommentInfo>> commentsByPath,
      Set<String> syncedMessages,
      Set<String> syncedComments,
      boolean skipAutogenerated) {

    Map<String, List<CommentInfo>> grouped = groupCommentsByMessage(commentsByPath);
    List<ReviewPlan> plans = new ArrayList<>();

    if (change.messages != null) {
      for (ChangeMessageInfo m : change.messages) {
        if (m.id == null) {
          continue;
        }
        List<CommentInfo> outstanding = new ArrayList<>();
        List<CommentInfo> forMessage = grouped.remove(m.id);
        if (forMessage != null) {
          for (CommentInfo c : forMessage) {
            if (!syncedComments.contains(c.id)) {
              outstanding.add(c);
            }
          }
        }
        boolean bodyPosted = syncedMessages.contains(m.id);
        if (bodyPosted && outstanding.isEmpty()) {
          continue;
        }
        if (skipAutogenerated && isAutogenerated(m) && outstanding.isEmpty()) {
          continue;
        }
        ReviewPlan plan = fill(new ReviewPlan(m.id, renderMessageBody(m), bodyPosted), outstanding);
        if (!plan.isEmpty()) {
          plans.add(plan);
        }
      }
    }

    // Comments whose change message is gone (deleted, or data predating
    // changeMessageId) still need archiving.
    List<String> orphanKeys = new ArrayList<>(grouped.keySet());
    orphanKeys.sort(Comparator.naturalOrder());
    for (String key : orphanKeys) {
      List<CommentInfo> outstanding = new ArrayList<>();
      for (CommentInfo c : grouped.get(key)) {
        if (!syncedComments.contains(c.id)) {
          outstanding.add(c);
        }
      }
      if (outstanding.isEmpty()) {
        continue;
      }
      plans.add(
          fill(
              new ReviewPlan(key, "_Review comments from Gerrit._", syncedMessages.contains(key)),
              outstanding));
    }

    return plans;
  }
}
