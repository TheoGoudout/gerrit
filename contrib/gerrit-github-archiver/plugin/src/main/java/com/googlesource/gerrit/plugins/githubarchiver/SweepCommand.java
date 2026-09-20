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

import com.google.gerrit.common.data.GlobalCapability;
import com.google.gerrit.extensions.annotations.RequiresCapability;
import com.google.gerrit.sshd.CommandMetaData;
import com.google.gerrit.sshd.SshCommand;
import com.google.inject.Inject;
import org.kohsuke.args4j.Option;

/**
 * Runs a reconciliation sweep on demand.
 *
 * <pre>
 *   ssh -p 29418 host github-archiver sweep
 *   ssh -p 29418 host github-archiver sweep --full
 * </pre>
 *
 * <p>{@code --full} ignores the lookback window and inspects every change, which is what to run at
 * cutover so changes still open at the end of the trial are archived too.
 */
@RequiresCapability(GlobalCapability.ADMINISTRATE_SERVER)
@CommandMetaData(name = "sweep", description = "Reconcile Gerrit changes onto GitHub")
public class SweepCommand extends SshCommand {

  @Option(
      name = "--full",
      usage = "inspect every change, ignoring the lookback window (use at cutover)")
  private boolean full;

  private final ArchiverService service;

  @Inject
  SweepCommand(ArchiverService service) {
    this.service = service;
  }

  @Override
  protected void run() throws Exception {
    if (!service.isRunning()) {
      throw die("the archiver is not running; check the error log for why it did not start");
    }
    ArchiverService.SweepStats stats = service.sweep(full);
    stdout.println(stats.toString());
    for (String error : stats.errors.subList(0, Math.min(20, stats.errors.size()))) {
      stderr.println("error: " + error);
    }
  }
}
