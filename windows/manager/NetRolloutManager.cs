// NetRollout Manager — the Windows app for running NetRollout: a window with
// the status, the address and the actions, and a tray icon. The actions run
// bin\manage.ps1 (hidden) and show its output; the status comes from
// NetRollout's health endpoint on this computer. Update: a newer release
// (Updates.cs), its Setup downloaded, checked and run - it updates the install.
//
// C# 5 / .NET Framework 4.8 (built into Windows 10/11): build.ps1 compiles it
// with Windows' own csc.exe, no SDK needed. Lives next to manage.ps1 in
// the install folder's bin\ folder.
//
//   NetRollout Manager.exe              the window (and the tray icon)
//   NetRollout Manager.exe --tray       only the tray icon (start at sign-in)
//   NetRollout Manager.exe --snapshot <file.png>   the window as an image
//                                                  (README screenshots)

using System;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.IO;
using System.Net;
using System.Net.Security;
using System.Security.Cryptography.X509Certificates;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Forms;

[assembly: System.Reflection.AssemblyTitle("NetRollout Manager")]
[assembly: System.Reflection.AssemblyProduct("NetRollout")]
[assembly: System.Reflection.AssemblyCompany("Itamar Weinstein")]
[assembly: System.Reflection.AssemblyCopyright("GNU AGPL v3")]

namespace NetRollout
{
	enum State { Checking, Running, Attention, Stopped }

	static class Program
	{
		[STAThread]
		static int Main(string[] args)
		{
			string snapshot = null;
			bool tray = false;
			for (int i = 0; i < args.Length; i++)
			{
				if (args[i] == "--tray") tray = true;
				if (args[i] == "--snapshot" && i + 1 < args.Length) snapshot = args[++i];
			}
			Application.EnableVisualStyles();
			Application.SetCompatibleTextRenderingDefault(false);
			Install.TrustLocalCertificate();
			if (snapshot != null)
			{
				using (var form = new ManagerForm(false))
				{
					form.Snapshot(snapshot);
				}
				return 0;
			}
			bool first;
			// one Manager per install folder (a second install - a test - has its own)
			string name = "NetRolloutManager-" + Install.Id;
			using (var mutex = new Mutex(true, name, out first))
			using (var showSignal = new EventWaitHandle(false, EventResetMode.AutoReset, name + ".Show"))
			{
				if (!first)
				{
					// one Manager: opening it again (Start Menu, Win+R, desktop)
					// brings the running one's window forward; --tray adds nothing
					if (!tray)
					{
						AllowSetForegroundWindow(ASFW_ANY);
						showSignal.Set();
					}
					return 0;
				}
				var form = new ManagerForm(tray);
				form.ShowWhenSignalled(showSignal);
				Application.Run(form);
			}
			return 0;
		}

		// the running Manager may take the foreground when this one hands it over
		const int ASFW_ANY = -1;
		[System.Runtime.InteropServices.DllImport("user32.dll")]
		static extern bool AllowSetForegroundWindow(int processId);
	}

	// A newer version: what's new, what happens, Update now (Enter) or Not now
	class UpdateDialog : Form
	{
		public UpdateDialog(Release release, string running)
		{
			Text = "Update NetRollout";
			Icon = Install.AppIcon(32);
			Font = new Font("Segoe UI", 9.5f);
			ClientSize = new Size(560, 420);
			MinimumSize = new Size(460, 360);
			StartPosition = FormStartPosition.CenterParent;
			ShowInTaskbar = false;
			MinimizeBox = MaximizeBox = false;
			var title = new Label { Text = "NetRollout " + release.Version + " is available", AutoSize = true,
				Font = new Font("Segoe UI Semibold", 13f), Location = new Point(16, 14) };
			var current = new Label { Text = "You have " + Install.Version + ".", AutoSize = true,
				ForeColor = Color.DimGray, Location = new Point(18, 44) };
			var notes = new TextBox { Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Vertical,
				Text = string.IsNullOrEmpty(release.Notes) ? "(no release notes)" : release.Notes.Replace("\r\n", "\n").Replace("\n", "\r\n"),
				Location = new Point(18, 72), Size = new Size(524, 196), BackColor = Color.FromArgb(248, 249, 250),
				Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right | AnchorStyles.Bottom };
			var what = new Label { AutoSize = false, Location = new Point(18, 278), Size = new Size(524, 60),
				Anchor = AnchorStyles.Left | AnchorStyles.Right | AnchorStyles.Bottom,
				Text = running + "A backup is made first. NetRollout keeps running while the new version downloads, " +
				       "then restarts - about a minute without it, and everyone signs in again." };
			var daily = new CheckBox { Text = "Check for updates daily", Checked = Updates.Daily, AutoSize = true,
				Location = new Point(18, 346), Anchor = AnchorStyles.Left | AnchorStyles.Bottom };
			daily.CheckedChanged += delegate { Updates.Daily = daily.Checked; };
			var update = new Button { Text = "Update now", DialogResult = DialogResult.OK, Size = new Size(120, 32),
				Location = new Point(290, 376), Anchor = AnchorStyles.Right | AnchorStyles.Bottom, FlatStyle = FlatStyle.System };
			var later = new Button { Text = "Not now", DialogResult = DialogResult.Cancel, Size = new Size(120, 32),
				Location = new Point(422, 376), Anchor = AnchorStyles.Right | AnchorStyles.Bottom, FlatStyle = FlatStyle.System };
			Controls.AddRange(new Control[] { title, current, notes, what, daily, update, later });
			AcceptButton = update;     // Enter updates
			CancelButton = later;
			Shown += delegate { update.Focus(); };
		}
	}

	// The install folder: this program sits in its bin\ folder
	static class Install
	{
		public static readonly string BinDir =
			Path.GetDirectoryName(Application.ExecutablePath);
		public static readonly string Root = Path.GetDirectoryName(BinDir);
		public static readonly string Script = Path.Combine(BinDir, "manage.ps1");
		public const string Releases = "https://github.com/itamar14-byte/NetRollout/releases";

		// this install, for names shared across the machine: the folder, hashed
		public static string Id
		{
			get
			{
				using (var sha = System.Security.Cryptography.SHA1.Create())
				{
					var hash = sha.ComputeHash(System.Text.Encoding.UTF8.GetBytes(Root.ToLowerInvariant()));
					return BitConverter.ToString(hash, 0, 8).Replace("-", "");
				}
			}
		}

		public static string Version
		{
			get { return Read(Path.Combine(Root, "VERSION")).Trim(); }
		}

		public static bool Installed
		{
			get { return File.Exists(Path.Combine(Root, ".env")); }
		}

		public static string Port
		{
			get { return Value(Path.Combine(Root, ".env"), "HTTPS_PORT", "443"); }
		}

		public static string Address
		{
			get
			{
				string host = Value(Path.Combine(Root, "config", "nginx", "site.env"),
					"NETROLLOUT_HOSTNAME", "localhost");
				return "https://" + host + (Port == "443" ? "" : ":" + Port);
			}
		}

		public static Icon AppIcon(int size)
		{
			string path = Path.Combine(BinDir, "netrollout.ico");
			try { return new Icon(path, size, size); }
			catch (Exception) { return Icon.ExtractAssociatedIcon(Application.ExecutablePath); }
		}

		static string Read(string path)
		{
			try { return File.ReadAllText(path); }
			catch (Exception) { return ""; }
		}

		static string Value(string path, string key, string fallback)
		{
			foreach (string line in Read(path).Split('\n'))
			{
				if (line.StartsWith(key + "="))
				{
					string v = line.Substring(key.Length + 1).Trim();
					if (v.Length > 0) return v;
				}
			}
			return fallback;
		}

		// NetRollout on this computer usually has a self-signed certificate:
		// accepted for 127.0.0.1 only, everything else verified as usual
		public static void TrustLocalCertificate()
		{
			ServicePointManager.SecurityProtocol = SecurityProtocolType.Tls12;
			ServicePointManager.ServerCertificateValidationCallback =
				delegate (object sender, X509Certificate cert, X509Chain chain, SslPolicyErrors errors)
				{
					var request = sender as HttpWebRequest;
					if (request != null && request.RequestUri.Host == "127.0.0.1") return true;
					return errors == SslPolicyErrors.None;
				};
		}

		// (state, the line shown under it)
		public static Tuple<State, string> Health()
		{
			if (!Installed) return Tuple.Create(State.Stopped, "Not installed here - run NetRollout Setup");
			try
			{
				var request = (HttpWebRequest)WebRequest.Create(
					"https://127.0.0.1:" + Port + "/_netrollout/health");
				request.Timeout = 4000;
				using (var response = (HttpWebResponse)request.GetResponse())
				using (var reader = new StreamReader(response.GetResponseStream()))
				{
					string json = reader.ReadToEnd();
					var m = Regex.Match(json, "\"running\"\\s*:\\s*(\\d+)");
					int running = m.Success ? int.Parse(m.Groups[1].Value) : 0;
					return Tuple.Create(State.Running, running > 0
						? running + " rollout" + (running == 1 ? "" : "s") + " running"
						: "Ready");
				}
			}
			catch (WebException e)
			{
				var response = e.Response as HttpWebResponse;
				if (response != null && (int)response.StatusCode == 503)
					return Tuple.Create(State.Attention, "The database or Redis isn't reachable - see Status");
				if (response != null)
					return Tuple.Create(State.Attention, "Answers with an error (" + (int)response.StatusCode + ") - see Status");
				return Tuple.Create(State.Stopped, "Not running");
			}
			catch (Exception)
			{
				return Tuple.Create(State.Stopped, "Not running");
			}
		}
	}

	class ManagerForm : Form
	{
		static readonly Color Dark = Color.FromArgb(18, 18, 18);
		static readonly Color Accent = Color.FromArgb(0, 188, 212);

		readonly Label stateLabel = new Label();
		readonly Label detailLabel = new Label();
		readonly Panel light = new Panel();
		readonly LinkLabel addressLink = new LinkLabel();
		readonly TextBox output = new TextBox();
		readonly Panel footer = new Panel();
		readonly FlowLayoutPanel buttons = new FlowLayoutPanel();
		readonly NotifyIcon trayIcon = new NotifyIcon();
		readonly System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer();
		readonly System.Windows.Forms.Timer updateTimer = new System.Windows.Forms.Timer();
		readonly Icon baseIcon = Install.AppIcon(32);
		State state = State.Checking;
		Process running;
		bool exiting, toldAboutTray, updating;
		Release announced;                  // the daily check's find, for a click on its notification
		ToolStripMenuItem dailyItem;
		readonly bool startInTray;

		public ManagerForm(bool tray)
		{
			startInTray = tray;
			Text = "NetRollout Manager";
			Icon = Install.AppIcon(32);
			ClientSize = new Size(860, 500);
			MinimumSize = new Size(600, 420);
			StartPosition = FormStartPosition.CenterScreen;
			Font = new Font("Segoe UI", 9.5f);
			BackColor = Color.White;

			// header: the logo on the app's dark background
			var header = new Panel { Dock = DockStyle.Top, Height = 76, BackColor = Dark };
			var logo = new PictureBox { Image = Install.AppIcon(48).ToBitmap(), SizeMode = PictureBoxSizeMode.CenterImage,
				Location = new Point(18, 14), Size = new Size(48, 48), BackColor = Dark };
			var title = new Label { Text = "NetRollout", ForeColor = Color.White, AutoSize = true,
				Font = new Font("Segoe UI Semibold", 17f), Location = new Point(76, 12), BackColor = Dark };
			var version = new Label { Text = "Manager  ·  version " + Install.Version, AutoSize = true,
				ForeColor = Color.FromArgb(150, 150, 150), Location = new Point(79, 46), BackColor = Dark };
			header.Controls.AddRange(new Control[] { logo, title, version });
			var stripe = new Panel { Dock = DockStyle.Top, Height = 3, BackColor = Accent };

			// status: a light, the state, the detail, the address
			var status = new Panel { Dock = DockStyle.Top, Height = 88, Padding = new Padding(18, 14, 18, 0) };
			light.Size = new Size(16, 16);
			light.Location = new Point(20, 20);
			light.Paint += PaintLight;
			stateLabel.Font = new Font("Segoe UI Semibold", 13f);
			stateLabel.AutoSize = true;
			stateLabel.Location = new Point(44, 12);
			detailLabel.AutoSize = true;
			detailLabel.ForeColor = Color.DimGray;
			detailLabel.Location = new Point(46, 40);
			addressLink.AutoSize = true;
			addressLink.Location = new Point(46, 62);
			addressLink.LinkClicked += delegate { OpenNetRollout(); };
			status.Controls.AddRange(new Control[] { light, stateLabel, detailLabel, addressLink });

			// actions
			buttons.Dock = DockStyle.Top;
			buttons.AutoSize = true;           // a narrow window: the buttons wrap, all visible
			buttons.AutoSizeMode = AutoSizeMode.GrowAndShrink;
			buttons.Padding = new Padding(14, 6, 14, 6);
			AddButton("Open NetRollout", delegate { OpenNetRollout(); }, true);
			AddButton("Start", delegate { Run("start", "Starting NetRollout..."); }, false);
			AddButton("Stop", delegate { StopNetRollout(); }, false);
			AddButton("Status", delegate { Run("status", "Checking everything..."); }, false);
			AddButton("Logs", delegate { Run("logs", "Recent log lines of the app:"); }, false);
			AddButton("Back up", delegate { Run("backup", "Backing up..."); }, false);
			AddButton("Restore...", delegate { RestoreBackup(); }, false);
			AddButton("Update", delegate { CheckForUpdates(true); }, false);

			// what the actions say
			output.Dock = DockStyle.Fill;
			output.Multiline = true;
			output.ReadOnly = true;
			output.ScrollBars = ScrollBars.Vertical;
			output.Font = new Font("Consolas", 9.5f);
			output.BackColor = Color.FromArgb(248, 249, 250);
			output.BorderStyle = BorderStyle.None;
			var outputBox = new Panel { Dock = DockStyle.Fill, Padding = new Padding(18, 4, 18, 8) };
			outputBox.Controls.Add(output);

			// one line; a long path shortened in the middle, as Explorer does
			footer.Dock = DockStyle.Bottom;
			footer.Height = 26;
			footer.Paint += delegate (object s, PaintEventArgs e)
			{
				TextRenderer.DrawText(e.Graphics, "Installed in " + Install.Root, Font,
					new Rectangle(18, 0, footer.Width - 30, footer.Height), Color.Gray,
					TextFormatFlags.PathEllipsis | TextFormatFlags.SingleLine |
					TextFormatFlags.VerticalCenter | TextFormatFlags.Left);
			};

			Controls.Add(outputBox);
			Controls.Add(buttons);
			Controls.Add(status);
			Controls.Add(stripe);
			Controls.Add(header);
			Controls.Add(footer);

			// the tray
			var menu = new ContextMenuStrip();
			var open = menu.Items.Add("Open NetRollout", null, delegate { OpenNetRollout(); });
			open.Font = new Font(menu.Font, FontStyle.Bold);
			menu.Items.Add("Show NetRollout Manager", null, delegate { ShowWindow(); });
			menu.Items.Add(new ToolStripSeparator());
			menu.Items.Add("Start", null, delegate { ShowWindow(); Run("start", "Starting NetRollout..."); });
			menu.Items.Add("Stop", null, delegate { ShowWindow(); StopNetRollout(); });
			menu.Items.Add(new ToolStripSeparator());
			menu.Items.Add("Check for updates", null, delegate { ShowWindow(); CheckForUpdates(true); });
			dailyItem = new ToolStripMenuItem("Check for updates daily") { Checked = Updates.Daily, CheckOnClick = true };
			dailyItem.CheckedChanged += delegate { Updates.Daily = dailyItem.Checked; };
			menu.Items.Add(dailyItem);
			menu.Items.Add(new ToolStripSeparator());
			menu.Items.Add("Exit", null, delegate { exiting = true; Close(); });
			trayIcon.ContextMenuStrip = menu;
			trayIcon.MouseClick += delegate (object s, MouseEventArgs e)
			{
				if (e.Button == MouseButtons.Left) ShowWindow();
			};
			trayIcon.Visible = true;

			timer.Interval = 15000;
			timer.Tick += delegate { RefreshStatus(); };
			timer.Start();

			// the daily check: a minute after start, then every day; a newer
			// version is announced in the tray (once a day), a click opens it
			updateTimer.Interval = 60 * 1000;
			updateTimer.Tick += delegate
			{
				updateTimer.Interval = 24 * 60 * 60 * 1000;
				if (Updates.Daily) CheckForUpdates(false);
			};
			updateTimer.Start();
			trayIcon.BalloonTipClicked += delegate
			{
				if (announced == null) return;
				ShowWindow();
				OfferUpdate(announced);
			};
			ShowState(State.Checking, "Checking...");
			Shown += delegate { RefreshStatus(); SayRecentUpdate(); };
		}

		protected override void SetVisibleCore(bool value)
		{
			// --tray: the window stays hidden until asked for
			if (startInTray && !IsHandleCreated)
			{
				CreateHandle();
				value = false;
				RefreshStatus();
			}
			base.SetVisibleCore(value);
		}

		void AddButton(string text, EventHandler click, bool primary)
		{
			var b = new Button { Text = text, AutoSize = true, Height = 32, Padding = new Padding(10, 2, 10, 2),
				Margin = new Padding(4), FlatStyle = FlatStyle.System };
			b.Click += click;
			if (primary) AcceptButton = b;
			buttons.Controls.Add(b);
		}

		void ShowWindow()
		{
			Show();
			if (WindowState == FormWindowState.Minimized) WindowState = FormWindowState.Normal;
			Activate();
		}

		// another launch of the Manager asks this one to show its window
		public void ShowWhenSignalled(EventWaitHandle signal)
		{
			var listener = new Thread(delegate ()
			{
				while (true)
				{
					signal.WaitOne();
					try { if (IsHandleCreated) BeginInvoke((MethodInvoker)ShowWindow); }
					catch (InvalidOperationException) { return; }   // closing
				}
			});
			listener.IsBackground = true;
			listener.Start();
		}

		protected override void OnFormClosing(FormClosingEventArgs e)
		{
			// the close button keeps it in the tray; Exit (tray menu) ends it
			if (!exiting && e.CloseReason == CloseReason.UserClosing)
			{
				e.Cancel = true;
				Hide();
				if (!toldAboutTray)
				{
					trayIcon.ShowBalloonTip(4000, "NetRollout Manager",
						"Still here, in the tray - right-click it for NetRollout's actions.", ToolTipIcon.Info);
					toldAboutTray = true;
				}
				return;
			}
			trayIcon.Visible = false;
			base.OnFormClosing(e);
		}

		void OpenNetRollout()
		{
			try { Process.Start(Install.Address); }
			catch (Exception e) { Say("Couldn't open the browser: " + e.Message); }
		}

		void StopNetRollout()
		{
			if (state == State.Running && detailLabel.Text.Contains("running") &&
				MessageBox.Show(this, detailLabel.Text + ". They finish and are recorded before NetRollout stops (up to 10 minutes). Stop now?",
					"Stop NetRollout", MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes)
				return;
			Run("stop", "Stopping NetRollout...");
		}

		// a backup from the backups folder (or anywhere), confirmed first
		void RestoreBackup()
		{
			string file;
			using (var pick = new OpenFileDialog
			{
				Title = "Restore NetRollout from a backup",
				Filter = "NetRollout backups (*.zip)|*.zip",
				InitialDirectory = Path.Combine(Install.Root, "backups")
			})
			{
				if (pick.ShowDialog(this) != DialogResult.OK) return;
				file = pick.FileName;
			}
			if (MessageBox.Show(this, "Restore " + Path.GetFileName(file) + "?\n\n" +
					"Everything in NetRollout since that backup is replaced, and everyone signs in again. " +
					"The current state is backed up first. Running rollouts finish before NetRollout stops.",
					"Restore NetRollout", MessageBoxButtons.YesNo, MessageBoxIcon.Warning,
					MessageBoxDefaultButton.Button2) != DialogResult.Yes)
				return;
			Run("restore \"" + file + "\" -Yes", "Restoring " + Path.GetFileName(file) + "...");
		}

		// ── updates ─────────────────────────────────────────────────────────
		// asked (the button, the tray menu): says what it found in the pane;
		// the daily check: only a newer version, in the tray
		void CheckForUpdates(bool asked)
		{
			if (updating) return;
			if (asked) { output.Clear(); Say("Checking for updates..."); }
			ThreadPool.QueueUserWorkItem(delegate
			{
				Release release = null;
				string problem = null;
				try { release = Updates.Latest(); }
				catch (Exception e) { problem = e.Message; }
				if (!IsHandleCreated) return;
				BeginInvoke((Action)delegate
				{
					if (problem != null)
					{
						if (asked) Say("Couldn't check for updates (" + problem + "). Is this computer online? " +
							"Releases: " + Install.Releases);
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
					trayIcon.ShowBalloonTip(10000, "NetRollout " + release.Version + " is available",
						"You have " + Install.Version + ". Click to see what's new and update.", ToolTipIcon.Info);
				});
			});
		}

		// opened again after an update (Update now): how it went, from its log
		void SayRecentUpdate()
		{
			var log = Path.Combine(Install.Root, @"logs\update.log");
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
			var running = state == State.Running && detailLabel.Text.Contains("running") ? detailLabel.Text + " - they finish first. " : "";
			using (var dialog = new UpdateDialog(release, running))
			{
				if (dialog.ShowDialog(this) != DialogResult.OK) return;
			}
			if (dailyItem != null) dailyItem.Checked = Updates.Daily;
			InstallUpdate(release);
		}

		void InstallUpdate(Release release)
		{
			updating = true;
			SetBusy(true);
			Say("Downloading " + release.SetupName + "...");
			ThreadPool.QueueUserWorkItem(delegate
			{
				string setup = null, problem = null;
				try { setup = Updates.Download(release, p => SayLater("   " + p + "%")); }
				catch (Exception e) { problem = e.Message; }
				BeginInvoke((Action)delegate
				{
					if (problem != null)
					{
						updating = false;
						SetBusy(false);
						Say("Not updated: " + problem + ".");
						return;
					}
					Say("Checked. Starting the update - NetRollout Manager closes now and opens again when it's done.");
					// Setup closes the Manager to replace it; whatever the outcome,
					// the Manager (the new one, if it's updated) opens afterwards
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

		// manage.ps1, hidden; its output in the pane
		void Run(string command, string heading)
		{
			if (running != null) { Say("Busy with the previous action - one moment."); return; }
			output.Clear();
			Say(heading);
			var info = new ProcessStartInfo("powershell.exe",
				"-NoProfile -ExecutionPolicy Bypass -File \"" + Install.Script + "\" " + command + " -NoBrowser")
			{
				UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = Install.Root,
				RedirectStandardOutput = true, RedirectStandardError = true
			};
			running = new Process { StartInfo = info, EnableRaisingEvents = true };
			running.OutputDataReceived += delegate (object s, DataReceivedEventArgs a) { if (a.Data != null) SayLater(a.Data); };
			running.ErrorDataReceived += delegate (object s, DataReceivedEventArgs a) { if (a.Data != null) SayLater(a.Data); };
			running.Exited += delegate
			{
				BeginInvoke((Action)delegate
				{
					running = null;
					SetBusy(false);
					RefreshStatus();
				});
			};
			SetBusy(true);
			try
			{
				running.Start();
				running.BeginOutputReadLine();
				running.BeginErrorReadLine();
			}
			catch (Exception e)
			{
				running = null;
				SetBusy(false);
				Say("Couldn't run " + Install.Script + ": " + e.Message);
			}
		}

		void SetBusy(bool busy)
		{
			foreach (Control c in buttons.Controls)
				if (c.Text != "Open NetRollout") c.Enabled = !busy;
			UseWaitCursor = busy;
		}

		void Say(string line) { output.AppendText(line + Environment.NewLine); }

		void SayLater(string line)
		{
			if (IsHandleCreated) BeginInvoke((Action)delegate { Say(line); });
		}

		void RefreshStatus()
		{
			ThreadPool.QueueUserWorkItem(delegate
			{
				var health = Install.Health();
				if (IsHandleCreated) BeginInvoke((Action)delegate { ShowState(health.Item1, health.Item2); });
			});
		}

		void ShowState(State s, string detail)
		{
			state = s;
			stateLabel.Text = s == State.Running ? "Running" : s == State.Attention ? "Needs attention"
				: s == State.Stopped ? "Stopped" : "Checking...";
			detailLabel.Text = detail;
			addressLink.Text = Install.Installed ? Install.Address : "";
			light.Invalidate();
			trayIcon.Text = "NetRollout - " + stateLabel.Text;
			trayIcon.Icon = TrayIcon(LightColor(s));
		}

		static Color LightColor(State s)
		{
			switch (s)
			{
				case State.Running: return Color.FromArgb(46, 160, 67);
				case State.Attention: return Color.FromArgb(230, 162, 0);
				case State.Stopped: return Color.FromArgb(207, 34, 46);
				default: return Color.Gray;
			}
		}

		void PaintLight(object sender, PaintEventArgs e)
		{
			e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
			using (var brush = new SolidBrush(LightColor(state)))
				e.Graphics.FillEllipse(brush, 1, 1, 13, 13);
		}

		// the logo with the state as a dot in its corner
		Icon TrayIcon(Color dot)
		{
			var bmp = new Bitmap(32, 32);
			using (var g = Graphics.FromImage(bmp))
			{
				g.SmoothingMode = SmoothingMode.AntiAlias;
				g.DrawIcon(baseIcon, new Rectangle(0, 0, 32, 32));
				using (var ring = new SolidBrush(Color.White)) g.FillEllipse(ring, 17, 17, 15, 15);
				using (var fill = new SolidBrush(dot)) g.FillEllipse(fill, 19, 19, 11, 11);
			}
			return Icon.FromHandle(bmp.GetHicon());
		}

		// the window as an image, after one status check (README screenshots)
		public void Snapshot(string path)
		{
			trayIcon.Visible = false;
			timer.Stop();
			// laid out only when shown: shown off-screen, drawn, closed
			StartPosition = FormStartPosition.Manual;
			Location = new Point(-20000, -20000);
			ShowInTaskbar = false;
			Show();
			var health = Install.Health();
			ShowState(health.Item1, health.Item2);
			Say("Status, Start, Stop and Logs show what they do here.");
			Application.DoEvents();
			var bmp = new Bitmap(Width, Height);
			DrawToBitmap(bmp, new Rectangle(0, 0, Width, Height));
			bmp.Save(path, System.Drawing.Imaging.ImageFormat.Png);
			exiting = true;
			Close();
		}
	}
}
