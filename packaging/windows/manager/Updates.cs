// NetRollout Manager's updates: is there a newer NetRollout, and its Setup
// downloaded and checked. The source is GitHub's releases - the installer is
// published there (Docker Hub gets the images first, before an installer
// exists) - on the installed version's channel, as Linux's update
// (src/setup/update.py): a release installed asks GitHub's latest release
// (never a pre-release: GitHub's latest leaves them out; a 404 there = none
// published yet); a pre-release installed (dev, a, b, rc) asks the release
// list and takes the newest by version, pre-releases included (drafts and
// tags that aren't a version skipped). An organisation's mirror (or a test)
// can stand in: HKCU\Software\NetRollout\Manager, UpdateFeed = a URL or a
// file with the same JSON - one release, used as given, or a list of them,
// picked as GitHub's list - its download links http(s) or file.
//
// Running the downloaded Setup (it updates the install) is the window's job.

using System;
using System.Collections;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Security.Cryptography;
using System.Text.RegularExpressions;
using System.Web.Script.Serialization;
using Microsoft.Win32;
using System.Drawing;
using System.Windows.Forms;

namespace NetRollout
{
	class Release
	{
		public string Version;
		public string Notes;
		public string Page;          // the release on GitHub
		public string SetupName;     // NetRollout-Setup-<version>.exe
		public string SetupUrl;
		public string SumsUrl;       // SHA256SUMS
	}

	// No release on the channel: none published on GitHub yet (its latest
	// answered 404 - only pre-releases exist - or its list has none)
	class NothingPublishedException : Exception
	{
		public NothingPublishedException() : base("No NetRollout release is published on GitHub yet") { }
	}

	static class Updates
	{
		public const string GitHubLatest = "https://api.github.com/repos/itamar14-byte/NetRollout/releases/latest";
		public const string GitHubList = "https://api.github.com/repos/itamar14-byte/NetRollout/releases?per_page=30";
		const string Key = @"Software\NetRollout\Manager";

		// the organisation's mirror or a test feed (UpdateFeed); null: GitHub
		public static string CustomFeed
		{
			get
			{
				var feed = Registry.GetValue(@"HKEY_CURRENT_USER\" + Key, "UpdateFeed", null) as string;
				return string.IsNullOrEmpty(feed) ? null : feed;
			}
		}

		// the daily check (on unless switched off: the tray menu, the update window)
		public static bool Daily
		{
			get
			{
				var value = Registry.GetValue(@"HKEY_CURRENT_USER\" + Key, "DailyCheck", null);
				return !(value is int) || (int)value != 0;
			}
			set { Registry.SetValue(@"HKEY_CURRENT_USER\" + Key, "DailyCheck", value ? 1 : 0, RegistryValueKind.DWord); }
		}

		// "<version>|<yyyy-MM-dd>": the daily check announces a version once a day
		public static string Announced
		{
			get { return Registry.GetValue(@"HKEY_CURRENT_USER\" + Key, "Announced", "") as string ?? ""; }
			set { Registry.SetValue(@"HKEY_CURRENT_USER\" + Key, "Announced", value); }
		}

		static WebClient Client()
		{
			var web = new WebClient();
			web.Headers.Add("User-Agent", "NetRollout-Manager/" + Install.Version);   // GitHub requires one
			web.Headers.Add("Accept", "application/vnd.github+json");
			return web;
		}

		static Uri Address(string feed)
		{
			// a plain path is a file
			return Regex.IsMatch(feed, "^[a-zA-Z][a-zA-Z0-9+.-]*://") ? new Uri(feed) : new Uri(Path.GetFullPath(feed));
		}

		// The newest release on the installed version's channel. Throws
		// NothingPublishedException (none on GitHub yet), WebException (no
		// connection, or an HTTP error) or InvalidDataException (an answer
		// that isn't a release).
		public static Release Newest(string installed)
		{
			bool prereleases = IsPreRelease(installed);
			var custom = CustomFeed;
			var where = custom ?? (prereleases ? GitHubList : GitHubLatest);
			string json;
			try
			{
				using (var web = Client()) json = web.DownloadString(Address(where));
			}
			catch (WebException e)
			{
				var http = e.Response as HttpWebResponse;
				if (custom == null && http != null && http.StatusCode == HttpStatusCode.NotFound)
					throw new NothingPublishedException();
				throw;
			}
			return FromJson(json, prereleases, custom == null);
		}

		// Why the check failed, in words (not NothingPublishedException: that's no failure)
		public static string Problem(Exception e)
		{
			var custom = CustomFeed;
			var web = e as WebException;
			if (web != null)
			{
				var http = web.Response as HttpWebResponse;
				var limited = http == null ? null : RateLimited(http);
				if (limited != null)
					return "Couldn't check for updates: " + limited;
				if (http != null)
					return "Couldn't check for updates: " + (custom ?? "GitHub") + " answered " + (int)http.StatusCode +
						" (" + http.StatusDescription + ").";
				if (custom != null && !Regex.IsMatch(custom, "^https?://", RegexOptions.IgnoreCase))
					return "Couldn't read the update feed " + custom + " (" + web.Message + ").";
				return "Couldn't check for updates (" + web.Message + "). Is this computer online?";
			}
			return "Couldn't check for updates (" + e.Message + ").";
		}

		// GitHub's limit for requests without a sign-in (60 an hour per internet
		// address, shared by everything behind it) in words: a 403 or 429 with
		// X-RateLimit-Remaining 0, the reset time (seconds since the epoch) in
		// X-RateLimit-Reset. null: another refusal. (src/setup/update.py's
		// rate_limited says the same for Linux.)
		static string RateLimited(HttpWebResponse http)
		{
			int code = (int)http.StatusCode;
			if ((code != 403 && code != 429) || http.Headers["X-RateLimit-Remaining"] != "0")
				return null;
			const string words = "GitHub's limit for update checks from this network (60 an hour per internet " +
				"address, shared by everything here that asks GitHub) is used up";
			long seconds;
			if (!long.TryParse(http.Headers["X-RateLimit-Reset"], out seconds))
				return words + ". Try again within the hour.";
			var reset = DateTimeOffset.FromUnixTimeSeconds(seconds).LocalDateTime;
			return words + ". It resets at " + reset.ToString("HH:mm") + ": try again then.";
		}

		// The release an answer gives: one release object, used as given, or a
		// list (GitHub's, a feed's): the newest by version - drafts and tags
		// that aren't a version skipped, pre-releases only when asked for.
		// github: a list with none is NothingPublishedException.
		public static Release FromJson(string json, bool prereleases, bool github)
		{
			var parsed = new JavaScriptSerializer().DeserializeObject(json);
			IDictionary data;
			var list = parsed as object[];
			if (list != null)
			{
				data = NewestIn(list, prereleases);
				if (data == null)
				{
					if (github) throw new NothingPublishedException();
					throw new InvalidDataException("the update feed lists no release" +
						(prereleases ? "" : " (a pre-release is offered only to a pre-release installed)"));
				}
			}
			else data = parsed as IDictionary;
			if (data == null || !(data["tag_name"] is string))
				throw new InvalidDataException("the answer isn't a release");
			var release = new Release
			{
				Version = ((string)data["tag_name"]).TrimStart('v'),
				Notes = data["body"] as string ?? "",
				Page = data["html_url"] as string ?? ""
			};
			release.SetupName = "NetRollout-Setup-" + release.Version + ".exe";
			var assets = data["assets"] as IEnumerable;
			if (assets != null)
				foreach (IDictionary asset in assets)
				{
					var name = asset["name"] as string;
					var url = asset["browser_download_url"] as string;
					if (name == release.SetupName) release.SetupUrl = url;
					if (name == "SHA256SUMS") release.SumsUrl = url;
				}
			return release;
		}

		static IDictionary NewestIn(object[] list, bool prereleases)
		{
			IDictionary best = null;
			string bestVersion = null;
			foreach (var item in list)
			{
				var release = item as IDictionary;
				if (release == null || true.Equals(release["draft"])) continue;
				var tag = release["tag_name"] as string;
				if (tag == null) continue;
				var version = tag.TrimStart('v');
				if (Parse(version) == null || (IsPreRelease(version) && !prereleases)) continue;
				if (best == null || IsNewer(version, bestVersion)) { best = release; bestVersion = version; }
			}
			return best;
		}

		// a dev, a, b or rc version; one that can't be read counts as a release
		public static bool IsPreRelease(string version)
		{
			var parts = Parse(version);
			return parts != null && parts[3] < 4;
		}

		// release order, as the setup core's (PEP 440 for our versions):
		// 1.0.0.dev0 < 1.0.0rc1 < 1.0.0 < 1.0.1; something else isn't newer
		public static bool IsNewer(string candidate, string installed)
		{
			var a = Parse(candidate);
			var b = Parse(installed);
			if (a == null || b == null) return false;
			for (int i = 0; i < a.Length; i++)
				if (a[i] != b[i]) return a[i] > b[i];
			return false;
		}

		static int[] Parse(string version)
		{
			var m = Regex.Match(version ?? "", @"^(\d+)\.(\d+)\.(\d+)(?:\.?(dev|a|b|rc)(\d+))?$");
			if (!m.Success) return null;
			int stage = 4;   // a release
			switch (m.Groups[4].Value)
			{
				case "dev": stage = 0; break;
				case "a": stage = 1; break;
				case "b": stage = 2; break;
				case "rc": stage = 3; break;
			}
			return new[] { int.Parse(m.Groups[1].Value), int.Parse(m.Groups[2].Value), int.Parse(m.Groups[3].Value),
				stage, m.Groups[5].Success ? int.Parse(m.Groups[5].Value) : 0 };
		}

		// The release's Setup into the temporary folder, checked against its
		// SHA256SUMS; returns its path. progress: 0-100. Throws WebException, or
		// InvalidDataException (a damaged or altered download - deleted).
		public static string Download(Release release, Action<int> progress)
		{
			if (release.SetupUrl == null || release.SumsUrl == null)
				throw new InvalidDataException("the release has no " + (release.SetupUrl == null ? release.SetupName : "SHA256SUMS") +
					" yet - try again later");
			string sums;
			using (var web = Client()) sums = web.DownloadString(Address(release.SumsUrl));
			string expected = null;
			foreach (var line in sums.Split('\n'))
			{
				var m = Regex.Match(line.Trim(), @"^([0-9a-fA-F]{64})\s+\*?(.+)$");
				if (m.Success && m.Groups[2].Value.Trim() == release.SetupName) expected = m.Groups[1].Value.ToLowerInvariant();
			}
			if (expected == null) throw new InvalidDataException("SHA256SUMS doesn't list " + release.SetupName);

			var path = Path.Combine(Path.GetTempPath(), release.SetupName);
			string actual;
			using (var web = Client())
			using (var source = web.OpenRead(Address(release.SetupUrl)))
			using (var target = File.Create(path))
			using (var sha = SHA256.Create())
			{
				long total = 0, done = 0;
				long.TryParse(web.ResponseHeaders != null ? web.ResponseHeaders["Content-Length"] : null, out total);
				var buffer = new byte[81920];
				int read, shown = -1;
				while ((read = source.Read(buffer, 0, buffer.Length)) > 0)
				{
					target.Write(buffer, 0, read);
					sha.TransformBlock(buffer, 0, read, null, 0);
					done += read;
					int percent = total > 0 ? (int)(done * 100 / total) : -1;
					if (percent >= 0 && percent / 10 != shown / 10) { shown = percent; progress(percent); }
				}
				sha.TransformFinalBlock(buffer, 0, 0);
				actual = BitConverter.ToString(sha.Hash).Replace("-", "").ToLowerInvariant();
			}
			if (actual != expected)
			{
				File.Delete(path);
				throw new InvalidDataException(release.SetupName + " doesn't match the release's checksum - the download is " +
					"damaged or was altered; it was deleted");
			}
			return path;
		}
	}

	// A newer version: what's new (and its page on GitHub), what happens,
	// Update now (Enter) or Not now
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
			// the release's page (full notes, downloads) - when the feed gives a web address
			if (Regex.IsMatch(release.Page ?? "", "^https?://", RegexOptions.IgnoreCase))
			{
				var page = new LinkLabel { Text = "View release on GitHub", AutoSize = true,
					Anchor = AnchorStyles.Top | AnchorStyles.Right };
				page.LinkClicked += delegate
				{
					try { Process.Start(release.Page); }
					catch (Exception e) { MessageBox.Show(this, "Couldn't open the browser: " + e.Message, Text); }
				};
				Controls.Add(page);    // first: its width in the window's font
				page.Location = new Point(ClientSize.Width - 18 - page.PreferredWidth, 44);   // the notes' right edge
			}
			AcceptButton = update;     // Enter updates
			CancelButton = later;
			Shown += delegate { update.Focus(); };
		}
	}
}
