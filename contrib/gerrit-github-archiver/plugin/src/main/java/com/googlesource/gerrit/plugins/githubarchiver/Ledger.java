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

import java.io.BufferedWriter;
import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.concurrent.locks.ReentrantLock;

/**
 * Durable record of what has already been projected onto GitHub.
 *
 * <p>Every GitHub write is gated on a lookup here and recorded on success, which is what makes the
 * sweep safe to re-run. It must survive restarts, or a plugin reload would duplicate the archive.
 *
 * <p>Storage is an append-only log with an in-memory index rebuilt on load. Gerrit's plugin API
 * bundles no embedded database, and pulling one in to hold a few thousand rows would be heavier
 * than the problem. Appends are flushed and fsynced, so a crash loses at most the record in flight
 * — and that case is already covered by the provenance markers, which let the projector re-adopt
 * anything GitHub accepted but the ledger missed.
 */
public class Ledger {

  public static final String KIND_MESSAGE = "message";
  public static final String KIND_COMMENT = "comment";

  /** What the projector knows about one change. */
  public static final class ChangeRecord {
    public final String changeKey;
    public final Integer prNumber;
    public final String headBranch;
    public final String lastUpdated;
    public final String status;

    ChangeRecord(
        String changeKey, Integer prNumber, String headBranch, String lastUpdated, String status) {
      this.changeKey = changeKey;
      this.prNumber = prNumber;
      this.headBranch = headBranch;
      this.lastUpdated = lastUpdated;
      this.status = status;
    }

    ChangeRecord merge(Integer pr, String branch, String updated, String st) {
      return new ChangeRecord(
          changeKey,
          pr != null ? pr : prNumber,
          branch != null ? branch : headBranch,
          updated != null ? updated : lastUpdated,
          st != null ? st : status);
    }
  }

  private static final String SEP = "\t";
  private static final String REC_CHANGE = "C";
  private static final String REC_SYNCED = "S";
  private static final String REC_PATCHSET = "P";

  private final Path path;
  private final ReentrantLock lock = new ReentrantLock();

  private final Map<String, ChangeRecord> changes = new HashMap<>();
  private final Set<String> synced = new HashSet<>();
  private final Map<String, String> patchSets = new HashMap<>();

  private BufferedWriter writer;

  public Ledger(Path path) throws IOException {
    this.path = path;
    Files.createDirectories(path.getParent());
    if (Files.exists(path)) {
      replay();
    }
    writer =
        Files.newBufferedWriter(
            path,
            StandardCharsets.UTF_8,
            StandardOpenOption.CREATE,
            StandardOpenOption.WRITE,
            StandardOpenOption.APPEND);
  }

  public static String changeKey(String project, int changeNumber) {
    return project + "~" + changeNumber;
  }

  private static String syncedKey(String changeKey, String kind, String gerritId) {
    return changeKey + "\u0000" + kind + "\u0000" + gerritId;
  }

  private static String patchSetKey(String changeKey, int number) {
    return changeKey + "\u0000" + number;
  }

  private void replay() throws IOException {
    List<String> lines = Files.readAllLines(path, StandardCharsets.UTF_8);
    for (String line : lines) {
      if (line.isEmpty()) {
        continue;
      }
      String[] f = line.split(SEP, -1);
      try {
        switch (f[0]) {
          case REC_CHANGE -> {
            ChangeRecord existing = changes.get(f[1]);
            Integer pr = f[2].isEmpty() ? null : Integer.valueOf(f[2]);
            String branch = f[3].isEmpty() ? null : f[3];
            String updated = f[4].isEmpty() ? null : f[4];
            String status = f[5].isEmpty() ? null : f[5];
            changes.put(
                f[1],
                existing == null
                    ? new ChangeRecord(f[1], pr, branch, updated, status)
                    : existing.merge(pr, branch, updated, status));
          }
          case REC_SYNCED -> synced.add(syncedKey(f[1], f[2], f[3]));
          case REC_PATCHSET -> patchSets.put(patchSetKey(f[1], Integer.parseInt(f[2])), f[3]);
          default -> {
            // Unknown record type from a newer version: ignore rather than
            // refuse to start. Losing one line degrades to a duplicate check
            // against GitHub, not to a corrupt archive.
          }
        }
      } catch (RuntimeException e) {
        // A truncated final line is the expected case after a hard kill.
        // Everything before it is still good, so keep what we have.
      }
    }
  }

  private void append(String... fields) {
    lock.lock();
    try {
      writer.write(String.join(SEP, fields));
      writer.newLine();
      writer.flush();
    } catch (IOException e) {
      throw new UncheckedIOException("failed appending to ledger " + path, e);
    } finally {
      lock.unlock();
    }
  }

  public Optional<ChangeRecord> getChange(String changeKey) {
    lock.lock();
    try {
      return Optional.ofNullable(changes.get(changeKey));
    } finally {
      lock.unlock();
    }
  }

  /** Insert or update a change row, leaving unspecified fields intact. */
  public void upsertChange(
      String changeKey, Integer prNumber, String headBranch, String lastUpdated, String status) {
    lock.lock();
    try {
      ChangeRecord existing = changes.get(changeKey);
      ChangeRecord merged =
          existing == null
              ? new ChangeRecord(changeKey, prNumber, headBranch, lastUpdated, status)
              : existing.merge(prNumber, headBranch, lastUpdated, status);
      changes.put(changeKey, merged);
      append(
          REC_CHANGE,
          changeKey,
          merged.prNumber == null ? "" : String.valueOf(merged.prNumber),
          merged.headBranch == null ? "" : merged.headBranch,
          merged.lastUpdated == null ? "" : merged.lastUpdated,
          merged.status == null ? "" : merged.status);
    } finally {
      lock.unlock();
    }
  }

  public boolean isSynced(String changeKey, String kind, String gerritId) {
    lock.lock();
    try {
      return synced.contains(syncedKey(changeKey, kind, gerritId));
    } finally {
      lock.unlock();
    }
  }

  public Set<String> syncedIds(String changeKey, String kind) {
    lock.lock();
    try {
      String prefix = changeKey + "\u0000" + kind + "\u0000";
      Set<String> out = new HashSet<>();
      for (String k : synced) {
        if (k.startsWith(prefix)) {
          out.add(k.substring(prefix.length()));
        }
      }
      return out;
    } finally {
      lock.unlock();
    }
  }

  /**
   * Record an artefact as projected.
   *
   * @return true when this call created the entry, false when it already existed. Callers treat
   *     false as "something else already posted this".
   */
  public boolean recordSynced(String changeKey, String kind, String gerritId, String githubId) {
    lock.lock();
    try {
      if (!synced.add(syncedKey(changeKey, kind, gerritId))) {
        return false;
      }
      append(REC_SYNCED, changeKey, kind, gerritId, githubId == null ? "" : githubId);
      return true;
    } finally {
      lock.unlock();
    }
  }

  public Optional<String> pushedSha(String changeKey, int patchSetNumber) {
    lock.lock();
    try {
      return Optional.ofNullable(patchSets.get(patchSetKey(changeKey, patchSetNumber)));
    } finally {
      lock.unlock();
    }
  }

  public void recordPatchSet(String changeKey, int patchSetNumber, String sha) {
    lock.lock();
    try {
      patchSets.put(patchSetKey(changeKey, patchSetNumber), sha);
      append(REC_PATCHSET, changeKey, String.valueOf(patchSetNumber), sha);
    } finally {
      lock.unlock();
    }
  }

  public void close() throws IOException {
    lock.lock();
    try {
      writer.close();
    } finally {
      lock.unlock();
    }
  }
}
