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

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonNull;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.io.IOException;
import java.net.ProxySelector;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Optional;
import java.util.concurrent.ThreadLocalRandom;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * GitHub REST and GraphQL client, scoped to what the archiver needs.
 *
 * <p>GitHub enforces two separate limits: a primary hourly quota surfaced through
 * {@code x-ratelimit-remaining}, and an undocumented secondary limit on content-creating requests
 * that appears as 403 or 429 with {@code retry-after}. Both are honoured. Archiving volume is low,
 * but a catch-up sweep after downtime trips the secondary limit easily.
 *
 * <p>Uses the JDK HTTP client and the Gson already on Gerrit's classpath, so the plugin adds no
 * dependencies of its own.
 */
public class GitHubClient {

  /** Raised for 422, which the projector treats as "this comment cannot be anchored". */
  public static class UnprocessableEntity extends IOException {
    private static final long serialVersionUID = 1L;

    UnprocessableEntity(String message) {
      super(message);
    }
  }

  public static class GitHubException extends IOException {
    private static final long serialVersionUID = 1L;

    GitHubException(String message) {
      super(message);
    }
  }

  private static final Pattern NEXT_LINK =
      Pattern.compile("<([^>]+)>\\s*;\\s*rel=\"next\"", Pattern.CASE_INSENSITIVE);
  private static final Gson GSON = new Gson();

  private final HttpClient http;
  private final String apiUrl;
  private final String token;
  private final String owner;
  private final String repo;
  private final int maxRetries;

  public GitHubClient(String apiUrl, String token, String owner, String repo) {
    this(apiUrl, token, owner, repo, 5);
  }

  public GitHubClient(String apiUrl, String token, String owner, String repo, int maxRetries) {
    this.apiUrl = apiUrl.replaceAll("/+$", "");
    this.token = token;
    this.owner = owner;
    this.repo = repo;
    this.maxRetries = maxRetries;
    this.http =
        HttpClient.newBuilder()
            .followRedirects(HttpClient.Redirect.NORMAL)
            .connectTimeout(Duration.ofSeconds(20))
            // Gerrit is commonly deployed behind an egress proxy.
            .proxy(ProxySelector.getDefault())
            .build();
  }

  public String slug() {
    return owner + "/" + repo;
  }

  private HttpRequest.Builder base(String url) {
    return HttpRequest.newBuilder(URI.create(url))
        .timeout(Duration.ofSeconds(60))
        .header("Authorization", "Bearer " + token)
        .header("Accept", "application/vnd.github+json")
        .header("X-GitHub-Api-Version", "2022-11-28")
        .header("User-Agent", "gerrit-github-archiver");
  }

  private long backoffMillis(HttpResponse<String> resp, int attempt) {
    Optional<String> retryAfter = resp.headers().firstValue("retry-after");
    if (retryAfter.isPresent()) {
      try {
        return (long) (Double.parseDouble(retryAfter.get()) * 1000);
      } catch (NumberFormatException ignored) {
        // Fall through to the computed backoff.
      }
    }
    if ("0".equals(resp.headers().firstValue("x-ratelimit-remaining").orElse(null))) {
      Optional<String> reset = resp.headers().firstValue("x-ratelimit-reset");
      if (reset.isPresent()) {
        try {
          long waitMs = (long) (Double.parseDouble(reset.get()) * 1000) - System.currentTimeMillis();
          return Math.max(0, waitMs) + 1000;
        } catch (NumberFormatException ignored) {
          // Fall through.
        }
      }
    }
    return Math.min(60_000L, 1000L * (1L << attempt))
        + ThreadLocalRandom.current().nextInt(1000);
  }

  /** Performs a request, retrying transient failures and rate limits. */
  public JsonElement request(String method, String path, JsonElement body) throws IOException {
    String url = path.startsWith("http") ? path : apiUrl + path;
    IOException last = null;
    for (int attempt = 0; attempt < maxRetries; attempt++) {
      HttpRequest.Builder b = base(url);
      if (body != null) {
        b.header("Content-Type", "application/json")
            .method(method, HttpRequest.BodyPublishers.ofString(GSON.toJson(body), StandardCharsets.UTF_8));
      } else {
        b.method(method, HttpRequest.BodyPublishers.noBody());
      }

      HttpResponse<String> resp;
      try {
        resp = http.send(b.build(), HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
      } catch (InterruptedException e) {
        Thread.currentThread().interrupt();
        throw new GitHubException("interrupted during " + method + " " + path);
      }

      int code = resp.statusCode();
      if (code == 422) {
        // Never retried: GitHub is objecting to the payload, not the timing.
        throw new UnprocessableEntity(method + " " + path + ": " + truncate(resp.body()));
      }
      if (code == 403 || code == 429 || code >= 500) {
        last = new GitHubException(code + ": " + truncate(resp.body()));
        sleep(backoffMillis(resp, attempt));
        continue;
      }
      if (code < 200 || code >= 300) {
        throw new GitHubException(method + " " + path + ": " + code + " " + truncate(resp.body()));
      }
      if (resp.body() == null || resp.body().isEmpty()) {
        // 204 and friends: a successful call with nothing to parse.
        return JsonNull.INSTANCE;
      }
      return JsonParser.parseString(resp.body());
    }
    throw new GitHubException(method + " " + path + " exhausted retries: " + last);
  }

  private static void sleep(long millis) throws GitHubException {
    try {
      Thread.sleep(millis);
    } catch (InterruptedException e) {
      Thread.currentThread().interrupt();
      throw new GitHubException("interrupted while backing off");
    }
  }

  private static String truncate(String s) {
    if (s == null) {
      return "";
    }
    return s.length() > 400 ? s.substring(0, 400) : s;
  }

  /** Follows {@code Link: rel="next"} until the collection is exhausted. */
  public List<JsonObject> paginate(String path) throws IOException {
    List<JsonObject> out = new ArrayList<>();
    String url = apiUrl + path + (path.contains("?") ? "&" : "?") + "per_page=100";
    while (url != null) {
      HttpResponse<String> resp;
      try {
        resp =
            http.send(
                base(url).GET().build(),
                HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
      } catch (InterruptedException e) {
        Thread.currentThread().interrupt();
        throw new GitHubException("interrupted paginating " + path);
      }
      if (resp.statusCode() < 200 || resp.statusCode() >= 300) {
        throw new GitHubException(
            "GET " + url + ": " + resp.statusCode() + " " + truncate(resp.body()));
      }
      JsonElement parsed = JsonParser.parseString(resp.body());
      if (parsed.isJsonArray()) {
        for (JsonElement e : parsed.getAsJsonArray()) {
          if (e.isJsonObject()) {
            out.add(e.getAsJsonObject());
          }
        }
      }
      url = nextLink(resp.headers().firstValue("link").orElse(null));
    }
    return out;
  }

  static String nextLink(String linkHeader) {
    if (linkHeader == null || linkHeader.isEmpty()) {
      return null;
    }
    Matcher m = NEXT_LINK.matcher(linkHeader);
    return m.find() ? m.group(1) : null;
  }

  // -- pull requests ---------------------------------------------------

  /**
   * Locates a pull request by head branch, open or closed.
   *
   * <p>This is the recovery path for a crash between creating a pull request and recording it.
   */
  public Optional<JsonObject> findPrByHead(String headBranch) throws IOException {
    String q =
        "/repos/"
            + slug()
            + "/pulls?state=all&head="
            + URLEncoder.encode(owner + ":" + headBranch, StandardCharsets.UTF_8);
    JsonElement res = request("GET", q, null);
    if (res.isJsonArray() && !res.getAsJsonArray().isEmpty()) {
      return Optional.of(res.getAsJsonArray().get(0).getAsJsonObject());
    }
    return Optional.empty();
  }

  public JsonObject createPr(String title, String head, String base, String body, boolean draft)
      throws IOException {
    JsonObject payload = new JsonObject();
    payload.addProperty("title", title);
    payload.addProperty("head", head);
    payload.addProperty("base", base);
    payload.addProperty("body", body);
    payload.addProperty("draft", draft);
    return request("POST", "/repos/" + slug() + "/pulls", payload).getAsJsonObject();
  }

  public JsonObject getPr(int number) throws IOException {
    return request("GET", "/repos/" + slug() + "/pulls/" + number, null).getAsJsonObject();
  }

  public JsonObject updatePr(int number, JsonObject fields) throws IOException {
    return request("PATCH", "/repos/" + slug() + "/pulls/" + number, fields).getAsJsonObject();
  }

  // -- reviews and comments --------------------------------------------

  public JsonObject createReview(int number, String body, JsonArray comments) throws IOException {
    JsonObject payload = new JsonObject();
    payload.addProperty("body", body);
    payload.addProperty("event", "COMMENT");
    if (comments != null && !comments.isEmpty()) {
      payload.add("comments", comments);
    }
    return request("POST", "/repos/" + slug() + "/pulls/" + number + "/reviews", payload)
        .getAsJsonObject();
  }

  public JsonObject createReviewComment(int number, JsonObject payload) throws IOException {
    return request("POST", "/repos/" + slug() + "/pulls/" + number + "/comments", payload)
        .getAsJsonObject();
  }

  public JsonObject createIssueComment(int number, String body) throws IOException {
    JsonObject payload = new JsonObject();
    payload.addProperty("body", body);
    return request("POST", "/repos/" + slug() + "/issues/" + number + "/comments", payload)
        .getAsJsonObject();
  }

  public List<JsonObject> listIssueComments(int number) throws IOException {
    return paginate("/repos/" + slug() + "/issues/" + number + "/comments");
  }

  public List<JsonObject> listReviewComments(int number) throws IOException {
    return paginate("/repos/" + slug() + "/pulls/" + number + "/comments");
  }

  public List<JsonObject> listReviews(int number) throws IOException {
    return paginate("/repos/" + slug() + "/pulls/" + number + "/reviews");
  }

  // -- GraphQL ---------------------------------------------------------

  /** Flips a draft pull request to ready. There is no REST equivalent. */
  public void markReadyForReview(String prNodeId) throws IOException {
    JsonObject vars = new JsonObject();
    vars.addProperty("id", prNodeId);
    JsonObject payload = new JsonObject();
    payload.addProperty(
        "query",
        "mutation($id: ID!) { markPullRequestReadyForReview(input: {pullRequestId: $id})"
            + " { clientMutationId } }");
    payload.add("variables", vars);
    JsonElement res = request("POST", apiUrl + "/graphql", payload);
    if (res.isJsonObject() && res.getAsJsonObject().has("errors")) {
      throw new GitHubException(
          "graphql: " + res.getAsJsonObject().get("errors").toString().toLowerCase(Locale.ROOT));
    }
  }
}
