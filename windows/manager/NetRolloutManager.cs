// NetRollout Manager — the Windows app for running NetRollout: a window with
// the status, the address and the actions, and a tray icon. The actions run
// windows\netrollout.ps1 (hidden) and show its output; the status comes from
// NetRollout's health endpoint on this computer.
//
// C# 5 / .NET Framework 4.8 (built into Windows 10/11): build.ps1 compiles it
// with Windows' own csc.exe, no SDK needed. Lives next to netrollout.ps1 in
// the install folder's windows\ folder.
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
			using (var mutex = new Mutex(true, "NetRolloutManager", out first))
			{
				if (!first)
				{
					MessageBox.Show("NetRollout Manager is already open (see the tray, next to the clock).",
						"NetRollout Manager", MessageBoxButtons.OK, MessageBoxIcon.Information);
					return 0;
				}
				Application.Run(new ManagerForm(tray));
			}
			return 0;
		}
	}

	// The install folder: this program sits in its windows\ folder
	static class Install
	{
		public static readonly string WindowsDir =
			Path.GetDirectoryName(Application.ExecutablePath);
		public static readonly string Root = Path.GetDirectoryName(WindowsDir);
		public static readonly string Script = Path.Combine(WindowsDir, "netrollout.ps1");

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
			string path = Path.Combine(WindowsDir, "netrollout.ico");
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
		readonly Icon baseIcon = Install.AppIcon(32);
		State state = State.Checking;
		Process running;
		bool exiting, toldAboutTray;
		readonly bool startInTray;

		public ManagerForm(bool tray)
		{
			startInTray = tray;
			Text = "NetRollout Manager";
			Icon = Install.AppIcon(32);
			ClientSize = new Size(680, 480);
			MinimumSize = new Size(560, 400);
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
			buttons.Height = 50;
			buttons.Padding = new Padding(14, 6, 14, 6);
			AddButton("Open NetRollout", delegate { OpenNetRollout(); }, true);
			AddButton("Start", delegate { Run("start", "Starting NetRollout..."); }, false);
			AddButton("Stop", delegate { StopNetRollout(); }, false);
			AddButton("Status", delegate { Run("status", "Checking everything..."); }, false);
			AddButton("Logs", delegate { Run("logs", "Recent log lines of the app:"); }, false);

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
			menu.Items.Add("Exit", null, delegate { exiting = true; Close(); });
			trayIcon.ContextMenuStrip = menu;
			trayIcon.DoubleClick += delegate { ShowWindow(); };
			trayIcon.Visible = true;

			timer.Interval = 15000;
			timer.Tick += delegate { RefreshStatus(); };
			timer.Start();
			ShowState(State.Checking, "Checking...");
			Shown += delegate { RefreshStatus(); };
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

		// netrollout.ps1, hidden; its output in the pane
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
