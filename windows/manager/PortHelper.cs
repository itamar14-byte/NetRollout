// NetRollout Manager's port helper (NetRollout Manager.exe --helper).

using System;
using System.Diagnostics;
using System.IO;
using System.Threading;
using System.Windows.Forms;

namespace NetRollout
{
	// The port helper (Windows): an HTTPS port saved in System Settings is
	// applied by itself. Headless - no window, no tray. It watches
	// config\nginx\site.env (change notifications, and a check every 3 s in
	// case one is missed) and, when there's a request to act on, runs
	// manage.ps1 apply hidden (the setup core decides; apply does the Docker
	// part) - output appended to logs\port-helper.log.
	class PortHelper : ApplicationContext
	{
		const int CheckMs = 3000;
		const int Pause = 15;      // seconds before an unchanged request is tried again
		readonly System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer();
		readonly Control invoker = new Control();
		readonly string site = Path.Combine(Install.Root, @"config\nginx\site.env");
		readonly string status = Path.Combine(Install.Root, @"config\apply-status.json");
		FileSystemWatcher watcher;
		Process running;
		DateTime lastEnd = DateTime.MinValue;
		string lastId = "";        // the request the last apply ran for
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
				if (changed || ticks * timer.Interval >= CheckMs) { ticks = 0; Check(); }
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

		void Check()
		{
			if (running != null && !running.HasExited) return;
			bool wasChanged = changed;
			changed = false;
			if (!Pending()) return;
			// a request apply left as it was (Docker not running, say): not every
			// 3 s - a new one at once (notifications may not come: Docker writes
			// site.env from inside the VM)
			string id = Value(site, "NETROLLOUT_PORT_REQUEST_ID");
			if (!wasChanged && id == lastId && (DateTime.Now - lastEnd).TotalSeconds < Pause) return;
			lastId = id;
			var log = Path.Combine(Install.Root, @"logs\port-helper.log");
			var info = new ProcessStartInfo("cmd.exe", "/c powershell.exe " +
				Install.PsArgs("apply -Yes -NoBrowser") + " >> \"" + log + "\" 2>&1")
			{
				UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = Install.Root
			};
			try
			{
				Directory.CreateDirectory(Path.GetDirectoryName(log));
				File.AppendAllText(log, Environment.NewLine + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " apply" + Environment.NewLine);
				running = Process.Start(info);
				running.EnableRaisingEvents = true;
				running.Exited += delegate { lastEnd = DateTime.Now; };
			}
			catch (Exception) { running = null; lastEnd = DateTime.Now; }
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

		static string Value(string path, string key)
		{
			try
			{
				foreach (var line in File.ReadAllLines(path))
					if (line.StartsWith(key + "=")) return line.Substring(key.Length + 1).Trim();
			}
			catch (IOException) { }
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
