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

import com.google.gerrit.extensions.events.ChangeAbandonedListener;
import com.google.gerrit.extensions.events.ChangeMergedListener;
import com.google.gerrit.extensions.events.ChangeRestoredListener;
import com.google.gerrit.extensions.events.CommentAddedListener;
import com.google.gerrit.extensions.events.LifecycleListener;
import com.google.gerrit.extensions.events.PrivateStateChangedListener;
import com.google.gerrit.extensions.events.RevisionCreatedListener;
import com.google.gerrit.extensions.events.WorkInProgressStateChangedListener;
import com.google.gerrit.extensions.registration.DynamicSet;
import com.google.inject.AbstractModule;

/** Guice bindings. Named by {@code Gerrit-Module} in the jar manifest. */
public class Module extends AbstractModule {

  @Override
  protected void configure() {
    bind(ArchiverConfig.class);
    bind(ArchiverService.class);

    // Starts the queue workers, opens the ledger and schedules the sweep.
    DynamicSet.bind(binder(), LifecycleListener.class).to(ArchiverService.class);

    DynamicSet.bind(binder(), CommentAddedListener.class).to(EventHandlers.class);
    DynamicSet.bind(binder(), RevisionCreatedListener.class).to(EventHandlers.class);
    DynamicSet.bind(binder(), ChangeMergedListener.class).to(EventHandlers.class);
    DynamicSet.bind(binder(), ChangeAbandonedListener.class).to(EventHandlers.class);
    DynamicSet.bind(binder(), ChangeRestoredListener.class).to(EventHandlers.class);
    DynamicSet.bind(binder(), WorkInProgressStateChangedListener.class).to(EventHandlers.class);
    DynamicSet.bind(binder(), PrivateStateChangedListener.class).to(EventHandlers.class);
  }
}
