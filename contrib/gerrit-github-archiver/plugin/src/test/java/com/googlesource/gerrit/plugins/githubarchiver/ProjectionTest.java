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

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import com.google.gerrit.extensions.client.Comment;
import com.google.gerrit.extensions.client.Side;
import com.google.gerrit.extensions.common.AccountInfo;
import com.google.gerrit.extensions.common.ChangeInfo;
import com.google.gerrit.extensions.common.ChangeMessageInfo;
import com.google.gerrit.extensions.common.CommentInfo;
import com.google.gerrit.extensions.common.ContextLineInfo;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.junit.Test;

public class ProjectionTest {

  private static final Timestamp WHEN =
      Timestamp.from(Instant.parse("2026-03-14T09:22:33Z"));

  private static AccountInfo account(int id, String name, String email) {
    AccountInfo a = new AccountInfo(id);
    a.name = name;
    a.email = email;
    return a;
  }

  private static CommentInfo comment(String id) {
    CommentInfo c = new CommentInfo();
    c.id = id;
    c.path = "src/a.java";
    c.patchSet = 1;
    c.updated = WHEN;
    c.message = "needs a null check";
    c.author = account(1000, "Alice Smith", "a@x.com");
    return c;
  }

  private static ChangeMessageInfo message(String id, String text) {
    ChangeMessageInfo m = new ChangeMessageInfo();
    m.id = id;
    m.date = WHEN;
    m.message = text;
    m._revisionNumber = 1;
    m.author = account(1000, "Alice Smith", "a@x.com");
    return m;
  }

  private static ChangeInfo change(ChangeMessageInfo... messages) {
    ChangeInfo c = new ChangeInfo();
    c._number = 42;
    c.project = "myproject";
    c.branch = "master";
    c.changeId = "I1234";
    c.subject = "Fix the thing";
    c.created = WHEN;
    c.owner = account(1000, "Alice Smith", "a@x.com");
    c.messages = List.of(messages);
    return c;
  }

  // -- anchoring -------------------------------------------------------

  @Test
  public void singleLineMapsToRightSide() {
    CommentInfo c = comment("c1");
    c.line = 42;
    Projection.Anchor a = Projection.anchorFor(c);
    assertNotNull(a);
    assertEquals(42, a.line());
    assertEquals("RIGHT", a.side());
    assertNull(a.startLine());
  }

  @Test
  public void parentSideMapsToLeft() {
    CommentInfo c = comment("c1");
    c.line = 42;
    c.side = Side.PARENT;
    assertEquals("LEFT", Projection.anchorFor(c).side());
  }

  @Test
  public void multiLineRangeEmitsStartLine() {
    CommentInfo c = comment("c1");
    c.range = new Comment.Range();
    c.range.startLine = 10;
    c.range.endLine = 14;
    Projection.Anchor a = Projection.anchorFor(c);
    assertEquals(14, a.line());
    assertEquals(Integer.valueOf(10), a.startLine());
    assertEquals("RIGHT", a.startSide());
  }

  @Test
  public void singleLineRangeOmitsStartLine() {
    // GitHub rejects start_line equal to line.
    CommentInfo c = comment("c1");
    c.range = new Comment.Range();
    c.range.startLine = 7;
    c.range.endLine = 7;
    Projection.Anchor a = Projection.anchorFor(c);
    assertEquals(7, a.line());
    assertNull(a.startLine());
  }

  @Test
  public void pseudoFilesAreNotAnchorable() {
    CommentInfo c = comment("c1");
    c.path = Projection.COMMIT_MSG;
    c.line = 3;
    assertNull(Projection.anchorFor(c));

    CommentInfo p = comment("c2");
    p.path = Projection.PATCHSET_LEVEL;
    assertNull(Projection.anchorFor(p));
  }

  @Test
  public void fileLevelCommentIsNotAnchorable() {
    assertNull(Projection.anchorFor(comment("c1")));
  }

  // -- markers ---------------------------------------------------------

  @Test
  public void inlineBodyCarriesCommentMarker() {
    CommentInfo c = comment("uuid-1");
    c.line = 4;
    String body = Projection.renderCommentBody(c);
    assertArrayEquals(
        new String[] {Ledger.KIND_COMMENT, "uuid-1"}, Projection.parseMarker(body));
    assertTrue(body.contains("Alice Smith"));
  }

  @Test
  public void fallbackBodyCarriesMarkerAndQuotesContext() {
    CommentInfo c = comment("uuid-2");
    c.path = Projection.COMMIT_MSG;
    ContextLineInfo ctx = new ContextLineInfo();
    ctx.lineNumber = 12;
    ctx.contextLine = "int x = null;";
    c.contextLines = List.of(ctx);
    String body = Projection.renderFallbackBody(c);
    assertArrayEquals(
        new String[] {Ledger.KIND_COMMENT, "uuid-2"}, Projection.parseMarker(body));
    assertTrue(body.contains("commit message"));
    assertTrue(body.contains("int x = null;"));
  }

  @Test
  public void messageBodyCarriesMessageMarker() {
    String body = Projection.renderMessageBody(message("msg-1", "Patch Set 1: Code-Review+2"));
    assertArrayEquals(
        new String[] {Ledger.KIND_MESSAGE, "msg-1"}, Projection.parseMarker(body));
    assertTrue(body.contains("Code-Review+2"));
  }

  @Test
  public void absentMarkerReturnsNull() {
    assertNull(Projection.parseMarker("plain body"));
    assertNull(Projection.parseMarker(null));
    assertNull(Projection.parseMarker(""));
  }

  // -- grouping --------------------------------------------------------

  @Test
  public void groupsByChangeMessageId() {
    CommentInfo c1 = comment("c1");
    c1.changeMessageId = "m1";
    CommentInfo c2 = comment("c2");
    c2.path = "b.java";
    c2.changeMessageId = "m1";
    CommentInfo c3 = comment("c3");
    c3.path = "b.java";
    c3.changeMessageId = "m2";

    Map<String, List<CommentInfo>> grouped =
        Projection.groupCommentsByMessage(
            Map.of("src/a.java", List.of(c1), "b.java", List.of(c2, c3)));
    assertEquals(2, grouped.get("m1").size());
    assertEquals(1, grouped.get("m2").size());
  }

  @Test
  public void injectsPathFromMapKey() {
    // The REST map keys by path and omits it from the value.
    CommentInfo c = new CommentInfo();
    c.id = "c";
    c.message = "m";
    c.changeMessageId = "m1";
    Map<String, List<CommentInfo>> grouped =
        Projection.groupCommentsByMessage(Map.of("dir/x.java", List.of(c)));
    assertEquals("dir/x.java", grouped.get("m1").get(0).path);
  }

  @Test
  public void synthesisesKeyWhenLinkMissing() {
    Map<String, List<CommentInfo>> grouped =
        Projection.groupCommentsByMessage(Map.of("src/a.java", List.of(comment("c1"))));
    assertTrue(grouped.keySet().iterator().next().startsWith("synthetic:"));
  }

  // -- planning --------------------------------------------------------

  @Test
  public void pairsCommentsWithTheirReview() {
    CommentInfo c = comment("c1");
    c.line = 3;
    c.changeMessageId = "m1";
    List<Projection.ReviewPlan> plans =
        Projection.planReviews(
            change(message("m1", "Patch Set 1: Code-Review+2")),
            Map.of("src/a.java", List.of(c)),
            Set.of(),
            Set.of(),
            true);
    assertEquals(1, plans.size());
    assertEquals("m1", plans.get(0).messageId);
    assertEquals(1, plans.get(0).inline.size());
  }

  @Test
  public void fullySyncedReviewIsFiltered() {
    // Re-running must be a no-op; this is the idempotency guarantee.
    CommentInfo c = comment("c1");
    c.line = 3;
    c.changeMessageId = "m1";
    assertTrue(
        Projection.planReviews(
                change(message("m1", "x")),
                Map.of("src/a.java", List.of(c)),
                Set.of("m1"),
                Set.of("c1"),
                true)
            .isEmpty());
  }

  @Test
  public void partiallySyncedReviewResumesItsComments() {
    // A crash between posting the body and its comments must be resumable.
    CommentInfo c1 = comment("c1");
    c1.line = 3;
    c1.changeMessageId = "m1";
    CommentInfo c2 = comment("c2");
    c2.line = 9;
    c2.changeMessageId = "m1";

    List<Projection.ReviewPlan> plans =
        Projection.planReviews(
            change(message("m1", "x")),
            Map.of("src/a.java", List.of(c1, c2)),
            Set.of("m1"),
            Set.of("c1"),
            true);
    assertEquals(1, plans.size());
    assertTrue(plans.get(0).bodyAlreadyPosted);
    assertEquals(List.of("c2"), plans.get(0).commentIds());
  }

  @Test
  public void autogeneratedMessageWithoutCommentsIsSkipped() {
    ChangeMessageInfo m = message("m1", "Uploaded patch set 2.");
    m.tag = "autogenerated:gerrit:newPatchSet";
    assertTrue(Projection.planReviews(change(m), Map.of(), Set.of(), Set.of(), true).isEmpty());
  }

  @Test
  public void autogeneratedMessageWithCommentsIsKept() {
    ChangeMessageInfo m = message("m1", "Patch Set 2:");
    m.tag = "autogenerated:gerrit:newPatchSet";
    CommentInfo c = comment("c1");
    c.line = 3;
    c.changeMessageId = "m1";
    assertEquals(
        1,
        Projection.planReviews(change(m), Map.of("src/a.java", List.of(c)), Set.of(), Set.of(), true)
            .size());
  }

  @Test
  public void autogeneratedKeptWhenFlagDisabled() {
    ChangeMessageInfo m = message("m1", "x");
    m.tag = "autogenerated:gerrit:merged";
    assertEquals(
        1, Projection.planReviews(change(m), Map.of(), Set.of(), Set.of(), false).size());
  }

  @Test
  public void unanchorableCommentsRouteToFallback() {
    CommentInfo c = comment("c1");
    c.path = Projection.COMMIT_MSG;
    c.line = 2;
    c.changeMessageId = "m1";
    List<Projection.ReviewPlan> plans =
        Projection.planReviews(
            change(message("m1", "x")),
            Map.of(Projection.COMMIT_MSG, List.of(c)),
            Set.of(),
            Set.of(),
            true);
    assertTrue(plans.get(0).inline.isEmpty());
    assertEquals(1, plans.get(0).fallback.size());
  }

  @Test
  public void orphanCommentsAreStillArchived() {
    // A comment whose change message was deleted must not be silently lost.
    CommentInfo c = comment("c1");
    c.line = 3;
    c.changeMessageId = "gone";
    List<Projection.ReviewPlan> plans =
        Projection.planReviews(change(), Map.of("src/a.java", List.of(c)), Set.of(), Set.of(), true);
    assertEquals(1, plans.size());
    assertEquals(List.of("c1"), plans.get(0).commentIds());
  }

  @Test
  public void orphanCommentsAreNotReposted() {
    CommentInfo c = comment("c1");
    c.line = 3;
    c.changeMessageId = "gone";
    assertTrue(
        Projection.planReviews(
                change(), Map.of("src/a.java", List.of(c)), Set.of("gone"), Set.of("c1"), true)
            .isEmpty());
  }

  @Test
  public void preservesGerritMessageOrder() {
    List<Projection.ReviewPlan> plans =
        Projection.planReviews(
            change(message("m1", "a"), message("m2", "b"), message("m3", "c")),
            Map.of(),
            Set.of(),
            Set.of(),
            true);
    assertEquals(List.of("m1", "m2", "m3"), plans.stream().map(p -> p.messageId).toList());
  }

  @Test
  public void prBodyLinksBackToGerrit() {
    String body = Projection.renderPrBody(change(), "https://gerrit.example.com/");
    assertTrue(body.contains("https://gerrit.example.com/c/myproject/+/42"));
    assertTrue(body.contains("I1234"));
    assertFalse(body.contains("//c/"));
  }
}
