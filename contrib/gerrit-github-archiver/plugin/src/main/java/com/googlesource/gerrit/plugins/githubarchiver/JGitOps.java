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

import com.google.gerrit.entities.Project;
import com.google.gerrit.server.git.GitRepositoryManager;
import java.io.IOException;
import java.util.Optional;
import java.util.logging.Logger;
import org.eclipse.jgit.api.Git;
import org.eclipse.jgit.api.errors.GitAPIException;
import org.eclipse.jgit.lib.ObjectId;
import org.eclipse.jgit.lib.Repository;
import org.eclipse.jgit.revwalk.RevCommit;
import org.eclipse.jgit.revwalk.RevWalk;
import org.eclipse.jgit.transport.RefSpec;
import org.eclipse.jgit.transport.UsernamePasswordCredentialsProvider;

/**
 * JGit-backed git operations against the repository Gerrit already has open.
 *
 * <p>This is where running in-process pays: patch set commits are in the Gerrit repository already,
 * so unlike the external archiver there is no mirror to clone and no fetch to keep current.
 */
public class JGitOps implements GitOps {

  private static final Logger log = Logger.getLogger(JGitOps.class.getName());

  private final GitRepositoryManager repoManager;
  private final String pushUrl;
  private final String token;

  public JGitOps(GitRepositoryManager repoManager, String pushUrl, String token) {
    this.repoManager = repoManager;
    this.pushUrl = pushUrl;
    this.token = token;
  }

  /**
   * {@inheritDoc}
   *
   * <p>Force is intentional: each patch set replaces the archive head so the final head SHA equals
   * the commit that lands on the target branch. That identity is what makes GitHub associate the
   * merge commit with the pull request, and in turn what makes the review reachable from {@code git
   * blame}.
   */
  @Override
  public void pushArchiveHead(String gerritProject, String sha, String branch) throws IOException {
    try (Repository repo = repoManager.openRepository(Project.nameKey(gerritProject));
        Git git = Git.wrap(repo)) {
      git.push()
          .setRemote(pushUrl)
          .setRefSpecs(new RefSpec(sha + ":refs/heads/" + branch))
          .setForce(true)
          .setCredentialsProvider(new UsernamePasswordCredentialsProvider("x-access-token", token))
          .call();
      log.info(() -> "pushed " + sha.substring(0, 10) + " -> " + branch);
    } catch (GitAPIException e) {
      throw new IOException("push of " + sha + " to " + branch + " failed", e);
    }
  }

  @Override
  public Optional<String> parentOf(String gerritProject, String sha) {
    try (Repository repo = repoManager.openRepository(Project.nameKey(gerritProject));
        RevWalk rw = new RevWalk(repo)) {
      RevCommit commit = rw.parseCommit(ObjectId.fromString(sha));
      return commit.getParentCount() > 0
          ? Optional.of(commit.getParent(0).name())
          : Optional.empty();
    } catch (IOException | IllegalArgumentException e) {
      return Optional.empty();
    }
  }
}
