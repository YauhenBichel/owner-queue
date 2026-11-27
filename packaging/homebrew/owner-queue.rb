# The Homebrew formula for owner-queue, kept here so that it is reviewed and tested with the code.
#
# It lives in the tap as Formula/owner-queue.rb. The `url` and `sha256` below are placeholders:
# scripts/render_formula.py fills them in from the sdist of a release, and the release workflow
# opens a pull request in the tap with the result (see RELEASING.md). Do not edit them by hand.
#
# What it installs is the `owner-queue` command only. The Claude Code plugin (the hooks that
# fill the queue, and /queue) is installed with `claude plugin`: see `caveats`.
class OwnerQueue < Formula
  include Language::Python::Virtualenv

  desc "One queue for what your Claude Code sessions leave for you"
  homepage "https://github.com/YauhenBichel/owner-queue"
  url "https://files.pythonhosted.org/packages/source/o/owner-queue/owner_queue-0.0.0.tar.gz"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  license "MIT"

  depends_on "python@3.14"

  def install
    virtualenv_install_with_resources
  end

  def caveats
    <<~EOS
      This installs the owner-queue command: next, list, done, skip, open, count, sync, add
      and statusline.

      The queue is filled by the hooks of the Claude Code plugin, which Homebrew does not
      install. Add it with:
        claude plugin marketplace add YauhenBichel/owner-queue
        claude plugin install owner-queue@owner-queue

      The queue is kept in ~/.claude/owner-queue (set OWNER_QUEUE_DIR to move it).
      Uninstalling the formula does not remove it.
    EOS
  end

  test do
    assert_equal "owner-queue #{version}", shell_output("#{bin}/owner-queue --version").strip

    ENV["OWNER_QUEUE_DIR"] = testpath/"queue"
    ENV["OWNER_QUEUE_NTFY"] = "off"
    assert_equal "0", shell_output("#{bin}/owner-queue count").strip
    assert_match(/^added \h+$/, shell_output("#{bin}/owner-queue add 'a note' --url https://example.com/x"))
    assert_equal "1", shell_output("#{bin}/owner-queue count").strip
    assert_match "https://example.com/x", shell_output("#{bin}/owner-queue next")
    assert_path_exists testpath/"queue/events.jsonl"
  end
end
