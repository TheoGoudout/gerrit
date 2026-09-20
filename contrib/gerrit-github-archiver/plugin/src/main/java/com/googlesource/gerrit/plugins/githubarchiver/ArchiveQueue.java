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

import java.util.LinkedHashMap;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.concurrent.locks.Condition;
import java.util.concurrent.locks.ReentrantLock;
import java.util.logging.Logger;

/**
 * A coalescing work queue keyed by change.
 *
 * <p>Two properties matter.
 *
 * <p><b>Coalescing.</b> A burst of events for one change — a patch set uploaded, then a review
 * posted seconds later — collapses into a single unit of work, because the worker re-reads full
 * state from Gerrit anyway.
 *
 * <p><b>Serialisation.</b> A change already being projected is never handed to a second worker. If
 * more events arrive while it is in flight it is re-queued once on completion, so the later state
 * is not lost.
 */
public class ArchiveQueue {

  private static final Logger log = Logger.getLogger(ArchiveQueue.class.getName());

  /** What the worker needs to project one change. */
  public record Target(String project, int changeNumber) {
    String key() {
      return project + "~" + changeNumber;
    }
  }

  private final ReentrantLock lock = new ReentrantLock();
  private final Condition notEmpty = lock.newCondition();
  private final LinkedHashMap<String, Target> pending = new LinkedHashMap<>();
  private final Set<String> inFlight = new HashSet<>();
  private final Map<String, Target> redo = new HashMap<>();
  private final int maxSize;

  private boolean closed;
  private long dropped;

  public ArchiveQueue(int maxSize) {
    this.maxSize = maxSize;
  }

  /** Enqueues a change. Returns false when coalesced, dropped or closed. */
  public boolean put(Target target) {
    lock.lock();
    try {
      if (closed) {
        return false;
      }
      String key = target.key();
      if (inFlight.contains(key)) {
        // Being worked on right now; remember to run it again afterwards.
        redo.put(key, target);
        return false;
      }
      if (pending.containsKey(key)) {
        pending.put(key, target);
        return false;
      }
      if (pending.size() >= maxSize) {
        // The scheduled sweep is the backstop, so shedding here is safe.
        dropped++;
        log.warning(
            () -> "queue full (" + maxSize + "); dropping " + key + ", the sweep will catch it");
        return false;
      }
      pending.put(key, target);
      notEmpty.signal();
      return true;
    } finally {
      lock.unlock();
    }
  }

  /** Claims the oldest queued change, blocking until one is available or the queue closes. */
  public Optional<Target> take() throws InterruptedException {
    lock.lock();
    try {
      while (pending.isEmpty() && !closed) {
        notEmpty.await();
      }
      if (pending.isEmpty()) {
        return Optional.empty();
      }
      var it = pending.entrySet().iterator();
      Map.Entry<String, Target> first = it.next();
      it.remove();
      inFlight.add(first.getKey());
      return Optional.of(first.getValue());
    } finally {
      lock.unlock();
    }
  }

  /** Releases a change, re-queueing it if events arrived while it was in flight. */
  public void done(Target target) {
    lock.lock();
    try {
      String key = target.key();
      inFlight.remove(key);
      Target again = redo.remove(key);
      if (again != null) {
        pending.put(key, again);
        notEmpty.signal();
      }
    } finally {
      lock.unlock();
    }
  }

  public void close() {
    lock.lock();
    try {
      closed = true;
      notEmpty.signalAll();
    } finally {
      lock.unlock();
    }
  }

  public int depth() {
    lock.lock();
    try {
      return pending.size();
    } finally {
      lock.unlock();
    }
  }

  public long droppedCount() {
    lock.lock();
    try {
      return dropped;
    } finally {
      lock.unlock();
    }
  }
}
