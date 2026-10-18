// NetRollout Manager: a release's notes (GitHub's markdown, the changelog's
// section and the release job's footer) as formatted text for the Upgrade
// window - headings, bullets, bold and code instead of ##, - and **. Only
// what the notes use: "## " / "### " headings, "- " / "* " bullets, **bold**,
// `code`, [text](link) (the text). The notes end at their "## Download"
// section: its table and links are for the release's web page, and the
// Manager downloads Setup itself.

using System;
using System.Text;
using System.Text.RegularExpressions;

namespace NetRollout
{
	static class ReleaseNotes
	{
		static readonly Regex Inline = new Regex(@"\*\*(.+?)\*\*|`([^`]+)`|\[([^\]]+)\]\([^)]+\)");

		// RTF for a RichTextBox: Segoe UI, code in Consolas
		public static string ToRtf(string markdown)
		{
			var rtf = new StringBuilder(@"{\rtf1\ansi\deff0{\fonttbl{\f0 Segoe UI;}{\f1 Consolas;}}" +
				@"{\colortbl;\red32\green32\blue32;\red90\green90\blue90;}\viewkind4\uc1\f0\fs19\cf1 ");
			bool first = true;
			foreach (string raw in (markdown ?? "").Replace("\r\n", "\n").Split('\n'))
			{
				string line = raw.TrimEnd();
				if (Regex.IsMatch(line, @"^##\s+Download\b", RegexOptions.IgnoreCase)) break;
				if (line.Length == 0) continue;
				Match heading = Regex.Match(line, @"^(#{1,6})\s+(.*)$");
				Match bullet = Regex.Match(line, @"^\s*[-*]\s+(.*)$");
				if (heading.Success)
				{
					// ## a section, ### its kind (Added, Fixed...)
					int size = heading.Groups[1].Length <= 2 ? 26 : 22;
					rtf.Append(@"\pard\sb" + (first ? 0 : 160) + @"\sa60\b\fs" + size + " ")
						.Append(Format(heading.Groups[2].Value)).Append(@"\b0\fs19\par ");
				}
				else if (bullet.Success)
				{
					rtf.Append(@"\pard\fi-200\li360\tx360\sa60 \bullet\tab ").Append(Format(bullet.Groups[1].Value)).Append(@"\par ");
				}
				else
				{
					rtf.Append(@"\pard\sa80 ").Append(Format(line)).Append(@"\par ");
				}
				first = false;
			}
			return rtf.Append("}").ToString();
		}

		// one line's text: **bold**, `code`, [text](link) - the rest escaped
		static string Format(string text)
		{
			var outText = new StringBuilder();
			int at = 0;
			foreach (Match m in Inline.Matches(text))
			{
				outText.Append(Escape(text.Substring(at, m.Index - at)));
				if (m.Groups[1].Success) outText.Append(@"\b ").Append(Format(m.Groups[1].Value)).Append(@"\b0 ");
				else if (m.Groups[2].Success) outText.Append(@"{\f1\fs18 ").Append(Escape(m.Groups[2].Value)).Append("}");
				else outText.Append(Format(m.Groups[3].Value));
				at = m.Index + m.Length;
			}
			return outText.Append(Escape(text.Substring(at))).ToString();
		}

		// RTF's own characters escaped; anything past ASCII as \uN?
		static string Escape(string text)
		{
			var outText = new StringBuilder();
			foreach (char c in text)
			{
				if (c == '\\' || c == '{' || c == '}') outText.Append('\\').Append(c);
				else if (c > 126) outText.Append(@"\u").Append((int)(short)c).Append('?');
				else outText.Append(c);
			}
			return outText.ToString();
		}
	}
}
