# Summarized:
# - moderates submission statement (recomment ss, report/remove)
# - moderates low effort flairs (removes outside casual friday)
# - reports unmoderated posts

import calendar
import config
from datetime import datetime, timedelta
from enum import Enum
import os
import praw
from settings import *
import time


def remove_comment(removal_reason, comment, settings, monitored_ss_replies):
    print(f"\tRemoving comment, reason: {removal_reason}")
    if settings.is_dry_run:
        print("\tDRY RUN!!!")
        return
    comment.mod.remove(mod_note=removal_reason)
    monitored_ss_replies.remove(comment.id)
    time.sleep(5)


class Post:
    def __init__(self, submission):
        self.submission = submission
        self.created_time = datetime.utcfromtimestamp(submission.created_utc)

    def __str__(self):
        return f"{self.submission.permalink} | {self.submission.title}"

    def has_low_effort_flair(self, settings):
        flair = self.submission.link_flair_text
        if not flair:
            return False
        if flair.lower() in settings.low_effort_flair:
            return True
        return False

    def submitted_during_casual_hours(self):
        # 00:00 Friday to 08:00 Saturday
        if self.created_time.isoweekday() == 5 or \
                (self.created_time.isoweekday() == 6 and self.created_time.hour < 8):
            return True
        return False

    def contains_report(self, report_substring, check_dismissed_reports):
        for report in self.submission.mod_reports:
            if any(report_substring in r for r in report):
                return True
        if check_dismissed_reports:
            # posts which haven't had dismissed reports don't contain the attr
            if hasattr(self.submission, "mod_reports_dismissed"):
                for report in self.submission.mod_reports_dismissed:
                    if report_substring in report[0]:
                        return True
        return False

    def contains_comment(self, text):
        for comment in self.submission.comments:
            # deleted comment
            if isinstance(comment.author, type(None)) or comment.removed:
                continue
            if text in comment.body:
                return True
        return False

    def is_post_old(self, time_mins):
        return self.created_time + timedelta(minutes=time_mins) < datetime.utcnow()

    def find_submission_statement(self):
        ss_candidates = []
        for comment in self.submission.comments:
            if comment.is_submitter:
                ss_candidates.append(comment)

        if len(ss_candidates) == 0:
            return None

        # use "ss" comment, otherwise longest
        submission_statement = ss_candidates[0]
        for candidate in ss_candidates:
            text = candidate.body.lower().strip().split()
            if ("submission" in text and "statement" in text) or ("ss" in text):
                submission_statement = candidate
                break
            if len(candidate.body) > len(submission_statement.body):
                submission_statement = candidate
        return submission_statement

    def is_moderator_approved(self):
        return self.submission.approved

    def is_removed(self):
        return self.submission.removed

    def report_post(self, settings, reason):
        print(f"\tReporting post, reason: {reason}")
        if settings.is_dry_run:
            print("\tDRY RUN!!!")
            return
        if self.contains_report(reason, True):
            print("\tPost has already been reported")
            return
        self.submission.report(reason)
        time.sleep(5)

    def reply_to_post(self, settings, reason, pin=True, lock=False):
        print(f"\tReplying to post, reason: {reason}")
        if settings.is_dry_run:
            print("\tDRY RUN!!!")
            return
        comment = self.submission.reply(reason)
        comment.mod.distinguish(sticky=pin)
        if lock:
            comment.mod.lock()
        time.sleep(5)

    @staticmethod
    def reply_to_comment(settings, original_comment, reason, lock=False, ignore_reports=False):
        print(f"\tReplying to comment, reason: {reason}")
        if settings.is_dry_run:
            print("\tDRY RUN!!!")
            return None
        reply_comment = original_comment.reply(reason)
        reply_comment.mod.distinguish()
        if ignore_reports:
            reply_comment.mod.ignore_reports()
        if lock:
            reply_comment.mod.lock()
        time.sleep(5)
        return reply_comment

    def remove_post(self, settings, reason, note):
        print(f"\tRemoving post, reason: {reason}")
        if settings.is_dry_run:
            print("\tDRY RUN!!!")
            return
        self.submission.mod.remove(spam=False, mod_note=note)
        removal_comment = self.submission.reply(reason)
        removal_comment.mod.distinguish(sticky=True)
        time.sleep(5)


class SubmissionStatementState(str, Enum):
    MISSING = "MISSING"
    TOO_SHORT = "TOO_SHORT"
    VALID = "VALID"


def ss_final_reminder(settings, post, submission_statement, submission_statement_state,
                      reminder_timeout_mins, timeout_mins):
    if not settings.submission_statement_final_reminder:
        return
    # only applies to posts that are between the time to remind and time to post
    if not post.is_post_old(reminder_timeout_mins) or post.is_post_old(timeout_mins):
        return
    if submission_statement_state == SubmissionStatementState.VALID:
        return
    reminder_identifier = "As a final reminder, your post must include a valid submission statement"
    if post.contains_comment(reminder_identifier):
        return

    # one final reminder to post an ss
    reminder_detail = "Your post is missing a submission statement." \
        if submission_statement_state == SubmissionStatementState.MISSING \
        else f"The submission statement I identified is too short ({len(submission_statement.body)}" \
             f" chars):\n> {submission_statement.body} \n\n" \
             f"https://old.reddit.com{submission_statement.permalink}"
    reminder_response = f"{reminder_identifier} within {timeout_mins} min. {reminder_detail}\n\n" \
                        f"{settings.submission_statement_rule_description}.\n\n" \
                        "Please message the moderators if you feel this was an error. " \
                        "Responses to this comment are not monitored."
    post.reply_to_post(settings, reminder_response, pin=False, lock=True)


class Janitor:
    def __init__(self):
        # get config from env vars if set, otherwise from config file
        client_id = os.environ["CLIENT_ID"] if "CLIENT_ID" in os.environ else config.CLIENT_ID
        client_secret = os.environ["CLIENT_SECRET"] if "CLIENT_SECRET" in os.environ else config.CLIENT_SECRET
        bot_username = os.environ["BOT_USERNAME"] if "BOT_USERNAME" in os.environ else config.BOT_USERNAME
        bot_password = os.environ["BOT_PASSWORD"] if "BOT_PASSWORD" in os.environ else config.BOT_PASSWORD

        if hasattr(config, "SUBREDDITS"):
            subreddits_config = os.environ["SUBREDDITS"] if "SUBREDDITS" in os.environ else config.SUBREDDITS
        else:
            subreddits_config = os.environ["SUBREDDIT"] if "SUBREDDIT" in os.environ else config.SUBREDDIT

        subreddit_names = [subreddit.strip() for subreddit in subreddits_config.split(",")]

        print("CONFIG: client_id=" + client_id + " client_secret=" + "*********" +
              " bot_username=" + bot_username + " bot_password=" + "*********" +
              " subreddit_names=" + str(subreddit_names))

        self.reddit = praw.Reddit(
            client_id=client_id,
            client_secret=client_secret,
            user_agent="my user agent",
            redirect_uri="http://localhost:8080",  # unused for script applications
            username=bot_username,
            password=bot_password
        )

        self.subreddit_names = subreddit_names
        self.time_unmoderated_last_checked = {}
        for subreddit in subreddit_names:
            self.time_unmoderated_last_checked[self.reddit.subreddit(subreddit)] = datetime.utcfromtimestamp(0)

        self.monitored_ss_replies = list()

    @staticmethod
    def get_adjusted_utc_timestamp(time_difference_mins):
        adjusted_utc_dt = datetime.utcnow() - timedelta(minutes=time_difference_mins)
        return calendar.timegm(adjusted_utc_dt.utctimetuple())

    def fetch_new_posts(self, settings, subreddit):
        check_posts_after_utc = self.get_adjusted_utc_timestamp(settings.post_check_threshold_mins)

        submissions = list()
        consecutive_old = 0
        # posts are provided in order of: newly submitted/approved (from automod block)
        for post in subreddit.new():
            if post.created_utc > check_posts_after_utc:
                submissions.append(Post(post))
                consecutive_old = 0
            # old, approved posts can show up in new amongst truly new posts due to reddit "new" ordering
            # continue checking new until consecutive_old_posts are checked, to account for these posts
            else:
                submissions.append(Post(post))
                consecutive_old += 1

            if consecutive_old > settings.consecutive_old_posts:
                return submissions
        return submissions

    def fetch_stale_unmoderated_posts(self, settings, subreddit_mod):
        check_posts_before_utc = self.get_adjusted_utc_timestamp(settings.stale_post_check_threshold_mins)

        stale_unmoderated = list()
        for post in subreddit_mod.unmoderated():
            # don't add posts which aren't old enough
            if post.created_utc < check_posts_before_utc:
                stale_unmoderated.append(Post(post))
        return stale_unmoderated

    @staticmethod
    def validate_submission_statement(settings, ss):
        if ss is None:
            return SubmissionStatementState.MISSING
        elif len(ss.body) < settings.submission_statement_minimum_char_length:
            return SubmissionStatementState.TOO_SHORT
        else:
            return SubmissionStatementState.VALID

    @staticmethod
    def handle_low_effort(settings, post):
        if not post.has_low_effort_flair(settings):
            return

        if not post.submitted_during_casual_hours():
            post.remove_post(settings, settings.casual_hour_removal_reason, "low effort flair")

    def handle_submission_statement(self, settings, post):
        # self posts don"t need a submission statement
        if post.submission.is_self:
            print("\tSelf post does not need a SS")
            return

        if post.contains_comment(settings.submission_statement_bot_prefix):
            print("\tBot has already posted SS")
            return

        # use link post's text if valid
        if post.submission.selftext != '':
            if len(post.submission.selftext) < settings.submission_statement_minimum_char_length:
                print("\tPost has short post-based submission statement")
                text = "Hi, thanks for your contribution. It looks like you've included your submission statement " \
                       "directly in your post, which is fine, but it is too short (min 150 chars). \n\n" \
                       "You can either edit your post's text to >150 chars, or include a comment-based ss instead " \
                       "(which I would post shortly, if it meets submission statement requirements).\n" \
                       "Please message the moderators if you feel this was an error. " \
                       "Responses to this comment are not monitored."
                if not post.contains_comment(text):
                    post.reply_to_post(settings, text, pin=False, lock=True)
            else:
                print("\tPost has valid post-based submission statement, not doing anything")
                return

        submission_statement = post.find_submission_statement()
        submission_statement_state = Janitor.validate_submission_statement(settings, submission_statement)

        timeout_mins = settings.submission_statement_time_limit_mins
        reminder_mins = timeout_mins / 2

        self.ss_on_topic_check(settings, post, submission_statement, submission_statement_state, timeout_mins)
        ss_final_reminder(settings, post, submission_statement, submission_statement_state, reminder_mins, timeout_mins)

        # users are given time to post a submission statement
        if not post.is_post_old(timeout_mins):
            print("\tTime has not expired")
            return
        print("\tTime has expired")

        if submission_statement_state == SubmissionStatementState.MISSING:
            print("\tPost does NOT have submission statement")
            if post.is_moderator_approved():
                reason = "Moderator approved post, but there is no SS. Please double check."
                post.report_post(settings, reason)
            elif settings.report_submission_statement_timeout:
                reason = "Post has no submission statement after timeout. Please take a look."
                post.report_post(settings, reason)
            else:
                post.remove_post(settings, settings.ss_removal_reason, "No submission statement")
        elif submission_statement_state == SubmissionStatementState.TOO_SHORT:
            print("\tPost has too short submission statement")
            if settings.submission_statement_pin:
                post.reply_to_post(settings, settings.submission_statement_pin_text(submission_statement),
                                   pin=True, lock=True)
            reason = "Submission statement is too short"
            if post.is_moderator_approved():
                reason = "Moderator approved post, but SS is too short. Please double check."
                post.report_post(settings, reason)
            elif settings.report_submission_statement_insufficient_length:
                post.report_post(settings, reason)
            else:
                post.remove_post(settings, settings.ss_removal_reason, reason)
        elif submission_statement_state == SubmissionStatementState.VALID:
            print("\tPost has valid submission statement")
            if settings.submission_statement_pin:
                post.reply_to_post(settings, settings.submission_statement_pin_text(submission_statement),
                                   pin=True, lock=True)
        else:
            print("\tERROR: unsupported submission_statement_state")

    def ss_on_topic_check(self, settings, post, submission_statement, submission_statement_state, timeout_mins):
        # not enabled, or malformed (enabled, but missing keywords or response)
        if not settings.submission_statement_on_topic_reminder or \
                not settings.submission_statement_on_topic_keywords or \
                not settings.submission_statement_on_topic_response:
            return
        if post.is_post_old(timeout_mins):
            return
        if submission_statement_state == SubmissionStatementState.MISSING:
            return

        on_topic_identifier = "does not explain how this content is related"
        bot_comment = None
        for reply in submission_statement.replies:
            if on_topic_identifier in reply.body:
                bot_comment = reply

        if post.submission.approved:
            if bot_comment in self.monitored_ss_replies:
                removal_reason = "Removing ss reply due to approved post"
                post.remove_comment(removal_reason, bot_comment, settings, self.monitored_ss_replies)
            return

        if bot_comment:
            return

        text = submission_statement.body.lower()
        for keyword in settings.submission_statement_on_topic_keywords:
            if keyword in text:
                # submission statement contains keyword, is on topic
                return
        response_keyword = settings.submission_statement_on_topic_response
        response = "Hi, thanks for contributing and including this submission statement. However, " \
                   "your comment does not appear to explain how this content is related to " + response_keyword + ". " \
                   "Could you please edit this comment to include that, before 30 mins?" \
                   "\n\n" \
                   "If I am wrong and your ss does explain the " + response_keyword + " relation," \
                   " kindly ignore and/or downvote this comment. " \
                   "If your submission statement does not explain how this content is related to collapse" \
                   ", it may be removed. (Please remember that if your submission statement is mostly" \
                   " or entirely extracted from the linked article, it will be removed!)" \
                   "\n\n" \
                   "This is a bot. Replies will not receive responses. " \
                   "Please message the moderators if you feel this was an error."
        comment = post.reply_to_comment(settings, submission_statement, response, lock=True, ignore_reports=True)
        if comment is not None and settings.submission_statement_on_topic_check_downvotes:
            self.monitored_ss_replies.append(comment.id)

    def handle_posts(self, settings, subreddit):
        posts = self.fetch_new_posts(settings, subreddit)
        print("Checking " + str(len(posts)) + " posts")
        for post in posts:
            print(f"Checking post: {post.submission.title}\n\t{post.submission.permalink}")

            try:
                self.handle_low_effort(settings, post)
                self.handle_submission_statement(settings, post)
            except Exception as e:
                print(e)

    def handle_stale_unmoderated_posts(self, settings, subreddit_mod):
        now = datetime.utcnow()
        last_checked = self.time_unmoderated_last_checked[subreddit_mod.subreddit]
        if last_checked > now - timedelta(minutes=settings.stale_post_check_frequency_mins):
            return

        stale_unmoderated_posts = self.fetch_stale_unmoderated_posts(settings, subreddit_mod)
        print("__UNMODERATED__")
        for post in stale_unmoderated_posts:
            print(f"Checking unmoderated post: {post.submission.title}")
            if settings.report_stale_unmoderated_posts:
                reason = "This post is over " + str(round(settings.stale_post_check_threshold_mins / 60, 2)) + \
                         "hours old and has not been moderated. Please take a look!"
                post.report_post(settings, reason)
            else:
                print(f"Not reporting stale unmoderated post: {post.submission.title}\n\t{post.submission.permalink}")
        self.time_unmoderated_last_checked[subreddit_mod.subreddit] = now

    def handle_monitored_ss_replies(self, settings):
        if not settings.submission_statement_on_topic_check_downvotes:
            return

        for comment_id in list(self.monitored_ss_replies):
            comment = self.reddit.comment(id=comment_id)
            # deleted comment
            if comment is None or isinstance(comment.author, type(None)) or comment.removed:
                self.monitored_ss_replies.remove(comment_id)
                continue
            removal_score = settings.submission_statement_on_topic_removal_score
            if comment.score < removal_score:
                removal_reason = "Removing ss reply due to low score: " + str(comment.score)
                remove_comment(removal_reason, comment, settings, self.monitored_ss_replies)
            if comment.created_utc < self.get_adjusted_utc_timestamp(60*24):
                print("Comment is over 1 day old and has [: " + str(comment.score) + "] score. Not monitoring anymore.")
                self.monitored_ss_replies.remove(comment_id)


def get_subreddit_settings(subreddit_name):
    # use <SubredditName>Settings if exists, default to Settings
    settings_name = subreddit_name + "Settings"
    try:
        constructor = globals()[settings_name]
        return constructor()
    except KeyError:
        return Settings()


def run_forever():
    while True:
        try:
            janitor = Janitor()
            while True:
                for subreddit_name in janitor.subreddit_names:
                    try:
                        settings = get_subreddit_settings(subreddit_name)
                        print("____________________")
                        print("Checking Subreddit: " + subreddit_name + " with ["
                              + settings.__class__.__name__ + "] settings")

                        subreddit = janitor.reddit.subreddit(subreddit_name)
                        janitor.handle_posts(settings, subreddit)
                        janitor.handle_stale_unmoderated_posts(settings, subreddit.mod)
                        janitor.handle_monitored_ss_replies(settings)
                    except Exception as e:
                        print(e)
                time.sleep(Settings.post_check_frequency_mins * 60)
        except Exception as e:
            print(e)
        time.sleep(Settings.post_check_frequency_mins * 60)


def run_once():
    janitor = Janitor()
    for subreddit_name in janitor.subreddit_names:
        settings = get_subreddit_settings(subreddit_name)
        subreddit = janitor.reddit.subreddit(subreddit_name)
        janitor.handle_posts(settings, subreddit)
        janitor.handle_stale_unmoderated_posts(settings, subreddit.mod)


if __name__ == "__main__":
    # run_once()
    run_forever()
