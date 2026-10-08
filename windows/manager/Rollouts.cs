// NetRollout Manager: the rollouts a Stop or an Update waits for - listed by
// the running app (manage.ps1 rollouts -Json: python -m src.jobs in its
// container) - and the choice: wait for them, cancel them all now
// (manage.ps1 stop-now: the app's drain cancels them at once; devices being
// configured finish, every result is recorded), or don't stop.

using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Web.Script.Serialization;
using System.Windows.Forms;

namespace NetRollout
{
	class RolloutRow
	{
		public string User, Devices, State, Started;
	}

	enum RolloutChoice { Wait, CancelAll, DontStop }

	static class Rollouts
	{
		// the rollouts running or queued; null when NetRollout can't say (not
		// running, or a version without the list) - then the health's count
		// is all there is. Blocks for a few seconds: not on the window's thread.
		public static List<RolloutRow> Fetch()
		{
			string output;
			if (RunScript("rollouts -Json", out output) != 0) return null;
			string line = null;
			foreach (string l in output.Split('\n'))
				if (l.Trim().StartsWith("{")) line = l.Trim();
			if (line == null) return null;
			try
			{
				var data = new JavaScriptSerializer().DeserializeObject(line) as IDictionary;
				var list = data == null ? null : data["rollouts"] as IEnumerable;
				if (list == null) return null;
				var rows = new List<RolloutRow>();
				foreach (IDictionary r in list)
				{
					var started = r["started"] as string;
					rows.Add(new RolloutRow
					{
						User = Convert.ToString(r["user"]),
						Devices = Convert.ToString(r["devices"]),
						State = Convert.ToString(r["state"]),
						Started = string.IsNullOrEmpty(started) ? "-" : started.Replace('T', ' ')
					});
				}
				return rows;
			}
			catch (Exception) { return null; }
		}

		// the stop (or update) that follows cancels them at once. Blocks.
		public static bool StopNow()
		{
			string output;
			return RunScript("stop-now", out output) == 0;
		}

		// manage.ps1, hidden, its output captured; -1 when it didn't finish in time
		static int RunScript(string command, out string output)
		{
			var info = new ProcessStartInfo("powershell.exe", Install.PsArgs(command))
			{
				UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = Install.Root,
				RedirectStandardOutput = true, RedirectStandardError = true
			};
			output = "";
			var text = new System.Text.StringBuilder();
			try
			{
				using (var p = new Process { StartInfo = info })
				{
					p.OutputDataReceived += delegate (object s, DataReceivedEventArgs a)
					{
						if (a.Data != null) lock (text) text.AppendLine(a.Data);
					};
					p.ErrorDataReceived += delegate { };
					p.Start();
					p.BeginOutputReadLine();
					p.BeginErrorReadLine();
					if (!p.WaitForExit(60000))
					{
						try { p.Kill(); } catch (Exception) { }
						return -1;
					}
					p.WaitForExit();      // the output's last lines
					lock (text) output = text.ToString();
					return p.ExitCode;
				}
			}
			catch (Exception) { return -1; }
		}
	}

	// The rollouts running or queued (by, devices, state, started) and the
	// three ways on: Wait (Enter), Cancel all now, Don't stop / update (Esc)
	class RolloutsDialog : Form
	{
		public RolloutChoice Choice = RolloutChoice.DontStop;

		// action: "stop" or "update"
		public RolloutsDialog(List<RolloutRow> rows, string action)
		{
			bool update = action == "update";
			Text = update ? "Update NetRollout" : "Stop NetRollout";
			Icon = Install.AppIcon(32);
			Font = new Font("Segoe UI", 9.5f);
			ClientSize = new Size(600, 400);
			MinimumSize = new Size(520, 340);
			StartPosition = FormStartPosition.CenterParent;
			ShowInTaskbar = false;
			MinimizeBox = MaximizeBox = false;
			int n = rows.Count;
			var title = new Label { AutoSize = true, Font = new Font("Segoe UI Semibold", 13f), Location = new Point(16, 14),
				Text = n + " rollout" + (n == 1 ? " is" : "s are") + " running or queued" };
			var intro = new Label { AutoSize = false, Location = new Point(18, 46), Size = new Size(564, 22),
				Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right, ForeColor = Color.DimGray,
				Text = update ? "The update restarts NetRollout. What should happen to them?"
				              : "What should happen to them before NetRollout stops?" };
			var list = new ListView { View = View.Details, FullRowSelect = true, HeaderStyle = ColumnHeaderStyle.Nonclickable,
				Location = new Point(18, 72), Size = new Size(564, 150), MultiSelect = false,
				Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right | AnchorStyles.Bottom };
			list.Columns.Add("By", 180);
			list.Columns.Add("Devices", 70, HorizontalAlignment.Right);
			list.Columns.Add("State", 100);
			list.Columns.Add("Started", 190);
			foreach (var r in rows)
				list.Items.Add(new ListViewItem(new[] { r.User, r.Devices, r.State, r.Started }));
			var what = new Label { AutoSize = false, Location = new Point(18, 232), Size = new Size(564, 104),
				Anchor = AnchorStyles.Left | AnchorStyles.Right | AnchorStyles.Bottom,
				Text = "Wait: they finish and are recorded first (up to 10 minutes); new rollouts are paused meanwhile.\r\n\r\n" +
				       "Cancel all now: devices they haven't reached are skipped; a device being configured finishes " +
				       "first, and every result is recorded. Queued rollouts are cancelled either way.\r\n\r\n" +
				       (update ? "Don't update: nothing changes - update later." : "Don't stop: NetRollout keeps running.") };
			var wait = AddButton("Wait for them", RolloutChoice.Wait, 150);
			var cancel = AddButton("Cancel all now", RolloutChoice.CancelAll, 140);
			var dont = AddButton(update ? "Don't update" : "Don't stop", RolloutChoice.DontStop, 120);
			dont.Location = new Point(ClientSize.Width - 18 - dont.Width, 350);
			cancel.Location = new Point(dont.Left - 10 - cancel.Width, 350);
			wait.Location = new Point(cancel.Left - 10 - wait.Width, 350);
			Controls.AddRange(new Control[] { title, intro, list, what, wait, cancel, dont });
			AcceptButton = wait;      // Enter waits - as the scripts' default
			CancelButton = dont;
			Shown += delegate { wait.Focus(); };
		}

		Button AddButton(string text, RolloutChoice choice, int width)
		{
			var b = new Button { Text = text, Size = new Size(width, 32), FlatStyle = FlatStyle.System,
				Anchor = AnchorStyles.Right | AnchorStyles.Bottom };
			b.Click += delegate { Choice = choice; DialogResult = choice == RolloutChoice.DontStop ? DialogResult.Cancel : DialogResult.OK; };
			return b;
		}
	}
}
