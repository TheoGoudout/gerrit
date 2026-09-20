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

import com.google.gerrit.extensions.common.ChangeInfo;
import com.google.gerrit.extensions.events.ChangeAbandonedListener;
import com.google.gerrit.extensions.events.ChangeEvent;
import com.google.gerrit.extensions.events.ChangeMergedListener;
import com.google.gerrit.extensions.events.ChangeRestoredListener;
import com.google.gerrit.extensions.events.CommentAddedListener;
import com.google.gerrit.extensions.events.PrivateStateChangedListener;
import com.google.gerrit.extensions.events.RevisionCreatedListener;
import com.google.gerrit.extensions.events.WorkInProgressStateChangedListener;
import com.google.inject.Inject;
import com.google.inject.Singleton;

/**
 * Listeners that nudge the archiver when a change moves.
 *
 * <p>Each one does exactly one thing: hand a change id to the queue and return. No Gerrit API call,
 * no GitHub call, no I/O. These run on Gerrit's own threads, and blocking one on a GitHub round
 * trip would stall the review server — the central hazard of running in-process.
 *
 * <p>They are also not a correctness mechanism. The event carries no inline comments, the queue
 * sheds load under a burst, and a projection can throw. The scheduled sweep in {@link
 * ArchiverService} is what guarantees the archive converges; these only shorten the delay.
 */
@Singleton
public class EventHandlers
    implements CommentAddedListener,
        RevisionCreatedListener,
        ChangeMergedListener,
        ChangeAbandonedListener,
        ChangeRestoredListener,
        WorkInProgressStateChangedListener,
        PrivateStateChangedListener {

  private final ArchiverService service;

  @Inject
  EventHandlers(ArchiverService service) {
    this.service = service;
  }

  private void nudge(ChangeEvent event) {
    ChangeInfo change = event.getChange();
    if (change != null && change.project != null && change._number != null) {
      service.enqueue(change.project, change._number);
    }
  }

  @Override
  public void onCommentAdded(CommentAddedListener.Event event) {
    nudge(event);
  }

  @Override
  public void onRevisionCreated(RevisionCreatedListener.Event event) {
    nudge(event);
  }

  @Override
  public void onChangeMerged(ChangeMergedListener.Event event) {
    nudge(event);
  }

  @Override
  public void onChangeAbandoned(ChangeAbandonedListener.Event event) {
    nudge(event);
  }

  @Override
  public void onChangeRestored(ChangeRestoredListener.Event event) {
    nudge(event);
  }

  @Override
  public void onWorkInProgressStateChanged(WorkInProgressStateChangedListener.Event event) {
    nudge(event);
  }

  /**
   * A change turning private after it was archived is a disclosure no API call undoes.
   *
   * <p>Handled rather than ignored so the projector reaches its explicit check and logs loudly.
   */
  @Override
  public void onPrivateStateChanged(PrivateStateChangedListener.Event event) {
    nudge(event);
  }
}
