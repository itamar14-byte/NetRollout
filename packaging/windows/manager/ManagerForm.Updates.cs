// NetRollout Manager's window: its upgrade part - checking for a newer
// release, offering it, running its Setup (Updates.cs finds and checks it).

using System;
using System.Diagnostics;
using System.IO;
using System.Threading;
using System.Windows.Forms;

namespace NetRollout
{
	partial class ManagerForm
	{
		// ── upgrades ────────────────────────────────────────────────────────
		// asked (the button, the tray menu): says what it found in the pane;
		// the daily check: only a newer version, in the tray (nothing on a
		// problem, nothing newer or nothing published)
		void CheckForUpdates(bool asked)
		{
			if (updating) return;
			if (asked) { output.Clear(); Say("Checking for upgrades..."); }
			ThreadPool.QueueUserWorkItem(delegate
			{
				Release release = null;
				string problem = null;
				try { release = Updates.Newest(Install.Version); }
				catch (NothingPublishedException e) { problem = e.Message + "."; }
				catch (Exception e) { problem = Updates.Problem(e); }
				if (!IsHandleCreated) return;
				BeginInvoke((Action)delegate
				{
					if (problem != null)
					{
						if (asked) Say(problem + " Releases: " + Install.Releases);
						return;
					}
					if (!Updates.IsNewer(release.Version, Install.Version))
					{
						if (asked) Say("You have the latest version (" + Install.Version + ").");
						return;
					}
					if (asked) { OfferUpdate(release); return; }
					var today = release.Version + "|" + DateTime.Now.ToString("yyyy-MM-dd");
					if (Updates.Announced == today) return;
					Updates.Announced = today;
					announced = release;
					portBalloon = false;
					trayIcon.ShowBalloonTip(10000, "NetRollout " + release.Version + " is available",
						"You have " + Install.Version + ". Click to see what's new and upgrade.", ToolTipIcon.Info);
				});
			});
		}

		// opened again after an upgrade (Upgrade now): how it went, from its log
		void SayRecentUpdate()
		{
			var log = Path.Combine(Install.Root, @"logs\upgrade.log");
			try
			{
				if (!File.Exists(log) || (DateTime.Now - File.GetLastWriteTime(log)).TotalMinutes > 10) return;
				var lines = File.ReadAllLines(log);
				for (int i = lines.Length - 1; i >= 0; i--)
					if (lines[i].Trim().Length > 0) { Say(lines[i].Trim() + "  (" + log + ")"); return; }
			}
			catch (IOException) { }
		}

		void OfferUpdate(Release release)
		{
			Say("NetRollout " + release.Version + " is available (you have " + Install.Version + ").");
			using (var dialog = new UpdateDialog(release, ""))   // rollouts: asked next
			{
				if (dialog.ShowDialog(this) != DialogResult.OK) return;
			}
			if (dailyItem != null) dailyItem.Checked = Updates.Daily;
			if (state != State.Running && state != State.Attention) { InstallUpdate(release, false); return; }
			AskAboutRollouts("upgrade", delegate (bool cancelAll) { InstallUpdate(release, cancelAll); });
		}

		// cancelAll: the running rollouts are cancelled as the upgrade restarts
		// NetRollout - asked for once the Setup is downloaded and checked, so a
		// failed download leaves nothing behind
		void InstallUpdate(Release release, bool cancelAll)
		{
			updating = true;
			SetBusy(true);
			Say("Downloading " + release.SetupName + "...");
			ThreadPool.QueueUserWorkItem(delegate
			{
				string setup = null, problem = null;
				try { setup = Updates.Download(release, p => SayLater("   " + p + "%")); }
				catch (Exception e) { problem = e.Message; }
				if (problem == null && cancelAll)
				{
					SayLater("Asking NetRollout to cancel the running rollouts as it restarts...");
					if (!Rollouts.StopNow()) SayLater("Couldn't ask it to cancel them - they finish first.");
				}
				BeginInvoke((Action)delegate
				{
					if (problem != null)
					{
						updating = false;
						SetBusy(false);
						Say("Not upgraded: " + problem + ".");
						return;
					}
					Say("Checked. Starting the upgrade - NetRollout Manager closes now and opens again when it's done.");
					// Setup closes the Manager to replace it; whatever the outcome,
					// the Manager (the new one, if it's upgraded) opens afterwards
					var start = new ProcessStartInfo("cmd.exe", "/c \"\"" + setup + "\" /SILENT & start \"\" \"" +
						Path.Combine(Install.BinDir, "NetRollout Manager.exe") + "\"\"")
					{
						UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = Path.GetTempPath()
					};
					try { Process.Start(start); }
					catch (Exception e)
					{
						updating = false;
						SetBusy(false);
						Say("Couldn't start " + setup + ": " + e.Message);
						return;
					}
					exiting = true;
					Close();
				});
			});
		}
	}
}
