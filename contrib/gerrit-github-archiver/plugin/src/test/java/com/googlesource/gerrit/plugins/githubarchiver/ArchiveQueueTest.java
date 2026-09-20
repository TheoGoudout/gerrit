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

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import com.googlesource.gerrit.plugins.githubarchiver.ArchiveQueue.Target;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import org.junit.Test;

public class ArchiveQueueTest {

  private static Target t(int n) {
    return new Target("myproject", n);
  }

  @Test
  public void coalescesRepeatKeys() throws Exception {
    ArchiveQueue q = new ArchiveQueue(10);
    assertTrue(q.put(t(1)));
    assertFalse(q.put(t(1)));
    assertEquals(1, q.depth());
  }

  @Test
  public void preservesArrivalOrder() throws Exception {
    ArchiveQueue q = new ArchiveQueue(10);
    q.put(t(1));
    q.put(t(2));
    q.put(t(3));
    List<Integer> seen = new ArrayList<>();
    for (int i = 0; i < 3; i++) {
      seen.add(q.take().orElseThrow().changeNumber());
    }
    assertEquals(List.of(1, 2, 3), seen);
  }

  @Test
  public void inFlightKeyIsNotHandedOutTwice() throws Exception {
    ArchiveQueue q = new ArchiveQueue(10);
    q.put(t(1));
    q.take();
    assertFalse(q.put(t(1)));
    assertEquals(0, q.depth());
  }

  @Test
  public void eventsDuringFlightRequeueOnce() throws Exception {
    // A comment posted mid-projection must not be lost.
    ArchiveQueue q = new ArchiveQueue(10);
    q.put(t(1));
    Target claimed = q.take().orElseThrow();
    q.put(t(1));
    q.put(t(1));
    q.done(claimed);
    assertEquals(1, q.depth());
    assertEquals(1, q.take().orElseThrow().changeNumber());
  }

  @Test
  public void quietCompletionDoesNotRequeue() throws Exception {
    ArchiveQueue q = new ArchiveQueue(10);
    q.put(t(1));
    q.done(q.take().orElseThrow());
    assertEquals(0, q.depth());
  }

  @Test
  public void overflowShedsLoad() {
    ArchiveQueue q = new ArchiveQueue(2);
    assertTrue(q.put(t(1)));
    assertTrue(q.put(t(2)));
    assertFalse(q.put(t(3)));
    assertEquals(1, q.droppedCount());
    assertEquals(2, q.depth());
  }

  @Test
  public void closeReleasesWaiters() throws Exception {
    ArchiveQueue q = new ArchiveQueue(10);
    CountDownLatch released = new CountDownLatch(1);
    Thread waiter =
        new Thread(
            () -> {
              try {
                q.take();
                released.countDown();
              } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
              }
            });
    waiter.setDaemon(true);
    waiter.start();
    Thread.sleep(50);
    q.close();
    assertTrue("close() did not release the waiter", released.await(2, TimeUnit.SECONDS));
  }

  @Test
  public void closedQueueRejectsWork() {
    ArchiveQueue q = new ArchiveQueue(10);
    q.close();
    assertFalse(q.put(t(1)));
  }
}
