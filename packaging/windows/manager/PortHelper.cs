// NetRollout Manager's port helper (NetRollout Manager.exe --helper).

using System;
using System.Diagnostics;
using System.IO;
using System.Threading;
using System.Windows.Forms;

namespace NetRollout
{
	// The port helper (Windows): an HTTPS port saved in System Settings is
	// applied by itself. Headless - no window, no tray, never a dialog (an
	// error is written to the log). It watches config\nginx\site.env (change
	// notifications, and a check every 3 s in case one is missed) and, when
	// there's a request to act on, runs manage.ps1 apply hidden (the setup
	// core decides; apply does the Docker part) - its output appended to
	// logs\port-helper.log when it differs from the last run's.
	class PortHelper : ApplicationContext
	{
		const int CheckMs = 3000;
		// seconds before a request apply left as it was is tried again: 15,
		// then longer each time (Docker stopped, say) up to MaxPause
		const int FirstPause = 15, NextPause = 60, MaxPause = 300;
		static readonly string LogFile = Path.Combine(Install.Root, @"logs\port-helper.log");
		readonly System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer();
		readonly Control invoker = new Control();
		readonly string site = Path.Combine(Install.Root, @"config\nginx\site.env");
		readonly string status = Path.Combine(Install.Root, @"config\apply-status.json");
		readonly string runOutput = Path.Combine(Install.Root, @"logs\port-helper.run.txt");
		FileSystemWatcher watcher;
		Process running;
		DateTime lastEnd = DateTime.MinValue;
		string lastId = "";        // the request the last apply ran for
		int pause = FirstPause;
		string lastOutput = null;  // the last run's output (logged once while it repeats)
		volatile bool changed = true;

		public PortHelper(EventWaitHandle exit)
		{
			invoker.CreateControl();
			var folder = Path.GetDirectoryName(site);
			if (Directory.Exists(folder))
			{
				watcher = new FileSystemWatcher(folder, "site.env")
				{
					NotifyFilter = NotifyFilters.LastWrite | NotifyFilters.FileName | NotifyFilters.Size
				};
				FileSystemEventHandler mark = delegate { changed = true; };
				watcher.Changed += mark;
				watcher.Created += mark;
				watcher.Renamed += delegate { changed = true; };   // site.env is replaced whole
				watcher.EnableRaisingEvents = true;
			}
			timer.Interval = 500;
			int ticks = 0;
			timer.Tick += delegate
			{
				ticks++;
				if (changed || ticks * timer.Interval >= CheckMs)
				{
					ticks = 0;
					try { Check(); }
					catch (Exception e) { Log("port helper: " + e.GetType().Name + ": " + e.Message); }
				}
			};
			timer.Start();
			var listener = new Thread(delegate ()
			{
				exit.WaitOne();
				try { invoker.BeginInvoke((MethodInvoker)ExitThread); }
				catch (InvalidOperationException) { }
			});
			listener.IsBackground = true;
			listener.Start();
		}

		// A line in logs\port-helper.log, stamped; never throws
		public static void Log(string text)
		{
			try
			{
				Directory.CreateDirectory(Path.GetDirectoryName(LogFile));
				File.AppendAllText(LogFile, Environment.NewLine + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " +
					text + Environment.NewLine);
			}
			catch (Exception) { }
		}

		void Check()
		{
			if (running != null && !running.HasExited) return;
			bool wasChanged = changed;
			changed = false;
			if (!Pending()) return;
			// a request apply left as it was (Docker not running, say): not every
			// 3 s - after a pause that grows while nothing changes; a new one at
			// once (notifications may not come: Docker writes site.env from
			// inside the VM)
			string id = Value(site, "NETROLLOUT_PORT_REQUEST_ID");
			bool again = !wasChanged && id == lastId;
			if (again && (DateTime.Now - lastEnd).TotalSeconds < pause) return;
			pause = !again ? FirstPause : Math.Min(pause < NextPause ? NextPause : pause * 2, MaxPause);
			lastId = id;
			var info = new ProcessStartInfo("cmd.exe", "/c powershell.exe " +
				Install.PsArgs("apply -Yes -NoBrowser") + " > \"" + runOutput + "\" 2>&1")
			{
				UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = Install.Root
			};
			try
			{
				Directory.CreateDirectory(Path.GetDirectoryName(runOutput));
				running = Process.Start(info);
				running.EnableRaisingEvents = true;
				running.Exited += delegate
				{
					lastEnd = DateTime.Now;
					try { invoker.BeginInvoke((MethodInvoker)Ran); }
					catch (InvalidOperationException) { }
				};
			}
			catch (Exception e)
			{
				running = null;
				lastEnd = DateTime.Now;
				Log("apply couldn't be started: " + e.Message);
			}
		}

		// An apply ended: its output into the log - once while it says the same
		// (Docker stopped: one entry, not one every retry)
		void Ran()
		{
			string output;
			try { output = File.ReadAllText(runOutput).Trim(); }
			catch (Exception e) { output = "(its output couldn't be read: " + e.Message + ")"; }
			if (output == lastOutput) return;
			lastOutput = output;
			Log("apply" + Environment.NewLine + output);
		}

		// a request the helper hasn't finished with: a new id, or a trial running
		bool Pending()
		{
			string id = Value(site, "NETROLLOUT_PORT_REQUEST_ID");
			if (string.IsNullOrEmpty(id)) return false;
			try
			{
				var data = new System.Web.Script.Serialization.JavaScriptSerializer()
					.DeserializeObject(File.ReadAllText(status)) as System.Collections.IDictionary;
				if (data == null) return true;
				return (data["id"] as string) != id || (data["state"] as string) == "trying";
			}
			catch (Exception) { return true; }    // no answer yet
		}

		// a key's value in the file; "" when it can't be read (site.env being
		// replaced - IOException, UnauthorizedAccessException - or anything else)
		static string Value(string path, string key)
		{
			try
			{
				foreach (var line in File.ReadAllLines(path))
					if (line.StartsWith(key + "=")) return line.Substring(key.Length + 1).Trim();
			}
			catch (Exception) { }
			return "";
		}

		protected override void ExitThreadCore()
		{
			timer.Stop();
			if (watcher != null) watcher.Dispose();
			base.ExitThreadCore();
		}
	}
}
