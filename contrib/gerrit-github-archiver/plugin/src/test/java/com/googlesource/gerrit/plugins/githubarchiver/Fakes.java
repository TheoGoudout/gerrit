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

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import java.io.IOException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

/** In-memory doubles for GitHub and git. */
final class Fakes {

  private Fakes() {}

  static final class FakeGitOps implements GitOps {
    final List<String[]> pushes = new ArrayList<>();

    @Override
    public void pushArchiveHead(String project, String sha, String branch) {
      pushes.add(new String[] {sha, branch});
    }

    @Override
    public Optional<String> parentOf(String project, String sha) {
      return Optional.of("p".repeat(40));
    }
  }

  /**
   * Records every write so tests can assert on duplicates.
   *
   * <p>{@code rejectBatch} makes the batched review endpoint raise 422 the way GitHub does when a
   * comment falls outside a diff hunk.
   */
  static class FakeGitHub extends GitHubClient {
    final Map<Integer, JsonObject> prs = new LinkedHashMap<>();
    final List<JsonObject> reviews = new ArrayList<>();
    final List<JsonObject> reviewComments = new ArrayList<>();
    final List<JsonObject> issueComments = new ArrayList<>();
    final List<String> readyCalls = new ArrayList<>();

    private int nextId = 1;
    private final boolean rejectBatch;
    private final boolean rejectIndividual;

    FakeGitHub() {
      this(false, false);
    }

    FakeGitHub(boolean rejectBatch, boolean rejectIndividual) {
      super("https://api.github.test", "token", "o", "r");
      this.rejectBatch = rejectBatch;
      this.rejectIndividual = rejectIndividual;
    }

    private int id() {
      return ++nextId;
    }

    @Override
    public Optional<JsonObject> findPrByHead(String headBranch) {
      return prs.values().stream()
          .filter(pr -> headBranch.equals(pr.getAsJsonObject("head").get("ref").getAsString()))
          .findFirst();
    }

    @Override
    public JsonObject createPr(String title, String head, String base, String body, boolean draft) {
      int number = id();
      JsonObject pr = new JsonObject();
      pr.addProperty("number", number);
      pr.addProperty("node_id", "node-" + number);
      pr.addProperty("title", title);
      pr.addProperty("body", body);
      pr.addProperty("state", "open");
      pr.addProperty("draft", draft);
      JsonObject headObj = new JsonObject();
      headObj.addProperty("ref", head);
      headObj.addProperty("sha", "a".repeat(40));
      pr.add("head", headObj);
      JsonObject baseObj = new JsonObject();
      baseObj.addProperty("ref", base);
      pr.add("base", baseObj);
      prs.put(number, pr);
      return pr;
    }

    @Override
    public JsonObject getPr(int number) {
      return prs.get(number);
    }

    @Override
    public JsonObject updatePr(int number, JsonObject fields) {
      JsonObject pr = prs.get(number);
      for (String k : fields.keySet()) {
        pr.add(k, fields.get(k));
      }
      return pr;
    }

    @Override
    public JsonObject createReview(int number, String body, JsonArray comments) throws IOException {
      if (comments != null && !comments.isEmpty() && rejectBatch) {
        throw new UnprocessableEntity("line must be part of the diff");
      }
      JsonObject review = new JsonObject();
      review.addProperty("id", id());
      review.addProperty("body", body);
      reviews.add(review);
      if (comments != null) {
        for (var e : comments) {
          reviewComments.add(e.getAsJsonObject());
        }
      }
      return review;
    }

    @Override
    public JsonObject createReviewComment(int number, JsonObject payload) throws IOException {
      if (rejectIndividual) {
        throw new UnprocessableEntity("line must be part of the diff");
      }
      JsonObject created = payload.deepCopy();
      created.addProperty("id", id());
      reviewComments.add(created);
      return created;
    }

    @Override
    public JsonObject createIssueComment(int number, String body) {
      JsonObject created = new JsonObject();
      created.addProperty("id", id());
      created.addProperty("body", body);
      issueComments.add(created);
      return created;
    }

    @Override
    public List<JsonObject> listIssueComments(int number) {
      return new ArrayList<>(issueComments);
    }

    @Override
    public List<JsonObject> listReviewComments(int number) {
      return new ArrayList<>(reviewComments);
    }

    @Override
    public List<JsonObject> listReviews(int number) {
      return new ArrayList<>(reviews);
    }

    @Override
    public void markReadyForReview(String nodeId) {
      readyCalls.add(nodeId);
      prs.values().stream()
          .filter(pr -> nodeId.equals(pr.get("node_id").getAsString()))
          .forEach(pr -> pr.addProperty("draft", false));
    }
  }
}
