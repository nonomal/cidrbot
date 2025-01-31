import logging
import os
import sys
import json
import boto3
from boto3.dynamodb.conditions import Key
from webexteamssdk import WebexTeamsAPI
import requests
from github import Github
from wxt_cidrbot import git_api_handler
from wxt_cidrbot import dynamo_api_handler
from wxt_cidrbot import cidrbot


class gitwebhook:
    def __init__(self):
        # Initialize logging
        logging.basicConfig(level=os.environ.get("LOGLEVEL", "DEBUG"))
        self.logging = logging.getLogger()

        if 'WEBEX_TEAMS_ACCESS_TOKEN' in os.environ:
            self.wxt_access_token = os.getenv("WEBEX_TEAMS_ACCESS_TOKEN")
        else:
            logging.error("Environment variable WEBEX_TEAMS_ACCESS_TOKEN must be set")
            sys.exit(1)

        if "WEBEX_BOT_ID" in os.environ:
            self.webex_bot_id = os.getenv("WEBEX_BOT_ID")
        else:
            logging.error("Environment variable WEBEX_BOT_ID must be set")
            sys.exit(1)

        if "DYNAMODB_INSTALLATION_TABLE" in os.environ:
            self.db_installation_name = os.getenv("DYNAMODB_INSTALLATION_TABLE")
        else:
            logging.error("Environment variable DYNAMODB_INSTALLATION_TABLE must be set")
            sys.exit(1)

        if "DYNAMODB_ROOM_TABLE" in os.environ:
            self.db_room_name = os.getenv("DYNAMODB_ROOM_TABLE")
        else:
            logging.error("Environment variable DYNAMODB_ROOM_TABLE must be set")
            sys.exit(1)

        self.git_handle = git_api_handler.githandler()
        self.dynamo = dynamo_api_handler.dynamoapi()
        self.cidrbot = cidrbot.cidrbot()
        self.Api = WebexTeamsAPI()
        self.dynamodb = ""
        self.table = ''
        self.room_id = ''
        self.EMOJIS = {'RED_X': '&#10060;', 'GREEN_CHECK': '&#9989;'}

    def webhook_request(self, event):
        json_string = json.loads((event["body"]))
        is_draft = False
        installation_id = json_string['installation']['id']
        event_action = json_string['action']
        x_event_type = event['headers']['x-github-event']
        github_name = json_string['sender']['login']

        if "repository" in json_string:
            json_string['repository']['full_name'] = json_string['repository']['full_name'].lower()

        if "pull_request" in json_string:
            is_draft = bool(json_string['pull_request']['draft'])

        if event_action in ('added', 'removed'):
            event_info = self.check_installation(installation_id)

            room_id = event_info[0]['room_id']
            message_add_repo = ""
            message_remove_repo = ""

            self.dynamodb = boto3.resource('dynamodb')
            self.table = self.dynamodb.Table(self.db_room_name)

            for repo_added in json_string['repositories_added']:
                if len(repo_added) > 0:
                    repo = repo_added['full_name']
                    message_add_repo += f" - " + repo + "\n"
                    self.dynamo.edit_repo(room_id, repo, installation_id, "add")

            for repo_removed in json_string['repositories_removed']:
                if len(repo_removed) > 0:
                    repo = repo_removed['full_name']
                    message_remove_repo += f" - " + repo + "\n"
                    self.dynamo.edit_repo(room_id, repo, installation_id, "remove")

            room = self.Api.rooms.get(room_id)
            room_name = room.title

            message = f"Repos updated for room: {room_name} \n"

            if len(message_add_repo) > 0:
                message += f"**Added:**\n" + message_add_repo
            if len(message_remove_repo) > 0:
                message += f"**Removed:**\n" + message_remove_repo

            person_id = self.dynamo.get_webex_username(github_name, room_id)

            if len(person_id) > 0:
                self.Api.messages.create(toPersonId=person_id, markdown=message)
            else:
                self.Api.messages.create(roomId=room_id, markdown=message)

        elif event_action == 'deleted':
            removed_repos = self.delete_installation(installation_id)
            message_uninstall = "A Cidr installation was just removed from this room. \n The following repos are no longer avaliable \n"

            if len(removed_repos) > 0:
                for repo in removed_repos:
                    message_uninstall += f" - " + repo + "\n"
                self.Api.messages.create(self.room_id, markdown=message_uninstall)

        elif x_event_type in ('issues', 'pull_request'):
            if event_action == 'opened' and not is_draft:
                self.triage_issue(installation_id, json_string, x_event_type)
            elif event_action == 'closed' and x_event_type == 'pull_request':
                self.send_merged_message(installation_id, json_string)
            elif event_action == 'review_requested':
                self.send_review_message(installation_id, json_string, False, None)
            elif event_action == 'ready_for_review':
                self.triage_issue(installation_id, json_string, x_event_type, converted_from_draft=True)

        elif x_event_type == 'pull_request_review':
            state = json_string['review']['state']
            state = state.lower()

            if state == 'approved':
                self.send_approved_message(installation_id, json_string)

    def triage_issue(self, installation_id, json_string, x_event_type, converted_from_draft=False):
        event_info = self.check_installation(installation_id)
        issue_type = "Pull request"
        query_key = "pull_request"

        if converted_from_draft:
            pull_request_action_msg = "has been converted from draft in"
        else:
            pull_request_action_msg = "created in"

        # Issues and prs have different dict structures
        if x_event_type == 'issues':
            issue_type = "Issue"
            query_key = "issue"
            issue_num = json_string['issue']['number']
        else:
            issue_num = json_string['number']

        room_id = event_info[0]['room_id']

        try:
            triage_list = self.dynamo.get_triage(room_id)
            repos = self.dynamo.get_repositories(room_id)
        except Exception:
            self.logging.debug("Error retrieving triage users and/or repos")
            sys.exit(1)

        issue_title = json_string[query_key]['title']
        issue_url = json_string[query_key]['html_url']
        issue_user = json_string[query_key]['user']['login']
        repo_name = json_string['repository']['full_name']
        repo_url = json_string['repository']['html_url']

        hyperlink_format = f'<a href="{issue_url}">{issue_title}</a>'
        hyperlink_format_repo = f'<a href="{repo_url}">{repo_name}</a>'
        message = f"{issue_type} {hyperlink_format} {pull_request_action_msg} {hyperlink_format_repo} by {issue_user}. Performing automated triage:"

        token_dict = self.dynamo.get_repo_keys(room_id, repo_name)
        token = token_dict[repo_name]
        git_api = Github(token)
        issue = git_api.get_repo(repo_name).get_issue(int(issue_num))

        if issue.pull_request is not None:
            self.logging.debug("The issue is a pull request")
            reviewers = issue.as_pull_request().get_review_requests()

            num_code_owners = self.get_code_owners_count(reviewers)

            self.logging.debug("Number of code owners %s", num_code_owners)
            if num_code_owners > 0:
                self.send_codeowners_message(
                    issue, room_id, hyperlink_format, hyperlink_format_repo, issue_type, installation_id, json_string,
                    issue_user, pull_request_action_msg
                )
                return

        if len(triage_list) < 1:
            self.logging.debug("No triage users, sending update message and quitting")
            empty_triage_message = (
                f"{issue_type} {hyperlink_format} {pull_request_action_msg} {hyperlink_format_repo} by {issue_user}. \n"
                + f"- To assign this issue, use the following \n" +
                f"- **@Cidrbot assign {repo_name} {issue_num} me|Git username|Webex firstname**"
            )
            self.Api.messages.create(room_id, markdown=empty_triage_message)
            return

        URL = f'https://webexapis.com/v1/messages'
        headers = {'Authorization': 'Bearer ' + self.wxt_access_token, 'Content-type': 'application/json;charset=utf-8'}
        post_message = {'roomId': room_id, 'markdown': message}
        response = requests.post(URL, json=post_message, headers=headers)
        if response.status_code == 200:
            self.logging.debug("Message created successfully")
            msg_edit_id = json.loads(str(response.text))["id"]
        else:
            self.logging.debug("Status code %s | text %s", str(response.status_code), str(response.text))

        session = requests.Session()
        self.logging.debug("Starting triage")
        author_list = []
        user_issue_count = []

        for triage_list_user in triage_list:
            author_list.append(triage_list_user)
            self.logging.debug("Adding %s", triage_list_user)

        query_repo = ""
        for repo in repos:
            self.logging.debug("repo: %s", repo)
            query_repo += f" repo:{repo} "

        for author in author_list:
            self.logging.debug("Checking issue count for Author: %s", author)
            issue_query = f"state:open type:issue assignee:{author}" + query_repo
            full_issue_url = f"https://api.github.com/search/issues?q=" + issue_query

            pr_query = f"state:open type:pr review-requested:{author}" + query_repo
            full_pr_url = f"https://api.github.com/search/issues?q=" + pr_query

            issue_search = session.get(full_issue_url, headers={})
            issue_count = issue_search.json()['total_count']

            pr_search = session.get(full_pr_url, headers={})
            pr_count = pr_search.json()['total_count']

            issue_count_dict = {'issues': issue_count + pr_count, 'username': author}
            user_issue_count.append(issue_count_dict)

        issue_count_sorted = sorted(user_issue_count, key=lambda i: i['issues'])
        self.logging.debug(issue_count_sorted)

        user_to_assign = None
        self.git_handle.room_and_edit_id(room_id, None)

        self.assign_triage(
            issue_count_sorted, repo_name, issue_num, issue_user, room_id, msg_edit_id, hyperlink_format, user_to_assign
        )

    def assign_triage(
        self, issue_count_sorted, repo_name, issue_num, issue_user, room_id, msg_edit_id, hyperlink_format,
        user_to_assign
    ):
        for user in issue_count_sorted:
            if issue_user != user['username']:
                user_to_assign = user['username']
                self.logging.debug("Picking user with least issues: %s", user_to_assign)

                git_user_info = requests.get('https://api.github.com/users/' + user_to_assign)
                full_name = git_user_info.json()['name']

                reply_message = self.git_handle.git_assign(repo_name, issue_num, user_to_assign, 'assign', full_name)
                self.logging.debug("assigning result %s", reply_message)
                if 'Error: **invalid user**' in reply_message:
                    self.logging.debug("Invalid user, cannot assign %s: ", user_to_assign)
                    fail_triage_message = f"{user_to_assign} cannot be assigned because they do not have access to the repo/org, checking next user in triage list..."
                    self.Api.messages.create(room_id, markdown=fail_triage_message, parentId=msg_edit_id)
                    continue
                break

        if user_to_assign is None:
            no_triage_message = "Cannot find a valid triage member to assign. This is likely caused by the only triage user being the owner of this pr"
            self.Api.messages.create(room_id, markdown=no_triage_message, parentId=msg_edit_id)
        else:
            room_message = f"{hyperlink_format} successfully assigned to " + user_to_assign
            if isinstance(reply_message, list):
                if reply_message[1] is not None and reply_message[1] == 'notify user':
                    room_message = reply_message[3]

            self.Api.messages.create(room_id, markdown=room_message, parentId=msg_edit_id)

    def send_codeowners_message(
        self, issue, room_id, hyperlink_format, hyperlink_format_repo, issue_type, installation_id, json_string,
        issue_user, pull_request_action_msg
    ):
        issue = issue.as_pull_request()
        reviewers = issue.raw_data['requested_reviewers']

        self.logging.debug("Codeowners is active, sending update message and quitting")
        reviewer_message = ""
        for reviewer in reviewers:
            reviewer_message += reviewer['login'] + ', '
        reviewer_message = reviewer_message[:-2]
        #issue number here
        empty_triage_message = f"{issue_type} {hyperlink_format} {pull_request_action_msg} {hyperlink_format_repo} by {issue_user}. This {issue_type} is auto assigned to {reviewer_message} via codeowners"
        self.Api.messages.create(room_id, markdown=empty_triage_message)
        self.logging.debug("Sending message to code owners")
        self.send_review_message(installation_id, json_string, True, reviewers)

    def send_merged_message(self, installation_id, json_string):
        event_info = self.check_installation(installation_id)
        room_id = event_info[0]['room_id']

        if json_string['pull_request']['merged'] is True:
            issue_title = json_string['pull_request']['title']
            issue_url = json_string['pull_request']['html_url']
            merger_user = json_string['pull_request']['merged_by']['login']
            repo_name = json_string['repository']['full_name']
            repo_url = json_string['repository']['html_url']

            hyperlink_format = f'<a href="{issue_url}">{issue_title}</a>'
            hyperlink_format_repo = f'<a href="{repo_url}">{repo_name}</a>'
            # add issue number here?
            merged_message = f"Pull request {hyperlink_format} has been merged in {hyperlink_format_repo} by {merger_user}"
            self.Api.messages.create(room_id, markdown=merged_message)

    def send_review_message(self, installation_id, json_string, codeowners_status, reviewers):
        event_info = self.check_installation(installation_id)
        room_id = event_info[0]['room_id']
        assigned_reviewers = json_string['pull_request']['requested_reviewers']
        reviewer_count = len(assigned_reviewers)
        requester_message = " by " + json_string['pull_request']['user']['login']

        if reviewer_count > 0 and codeowners_status is False:
            self.logging.debug("Codeowners will handle message sending, quitting")
            return

        if codeowners_status:
            assigned_reviewers = reviewers
            requester_message = " via codeowners"

        for reviewer in assigned_reviewers:
            all_room_users = self.dynamo.user_dict(room_id)
            for room_user in all_room_users:
                if 'git_name' in all_room_users[room_user] and all_room_users[room_user]['git_name'] == reviewer['login'
                                                                                                                 ]:
                    user_id = all_room_users[room_user]['person_id']
                    user_name = all_room_users[room_user]['first_name']

                    if all_room_users[room_user]['reminders_enabled'] == "on":
                        issue_title = json_string['pull_request']['title']
                        issue_url = json_string['pull_request']['html_url']
                        repo_name = json_string['repository']['full_name']
                        repo_url = json_string['repository']['html_url']

                        hyperlink_format = f'<a href="{issue_url}">{issue_title}</a>'
                        hyperlink_format_repo = f'<a href="{repo_url}">{repo_name}</a>'
                        message = f"Hello {user_name}, you have been requested to review {hyperlink_format} in repo {hyperlink_format_repo}{requester_message}."
                        self.logging.debug("Sending message to %s \n message = %s", user_name, message)
                        self.cidrbot.send_directwbx_msg(user_id, message)

    def send_approved_message(self, installation_id, json_string):
        """
        Checks to make sure certain paramters are met before sending a message stating a repository is approved

        :param installation_id: string, id for installation
        :param json_string: string, the body of the webhook

        :return: Nothing
        """
        event_info = self.check_installation(installation_id)
        room_id = event_info[0]['room_id']
        pr_author = json_string['pull_request']['user']['login'].lower()
        branch_name = json_string['pull_request']['head']['ref']
        repo_name = json_string['pull_request']['head']['repo']['full_name']
        review_message = json_string['review']['body']

        self.logging.debug("review message: %s", review_message)

        token_dict = self.dynamo.get_repo_keys(room_id, repo_name)
        token = token_dict[repo_name]
        headers = {'Authorization': 'token ' + token}

        all_room_users = self.dynamo.user_dict(room_id)
        reminders_enabled = None
        for room_user in all_room_users:
            if all_room_users[room_user]['git_name'] == pr_author:
                user_id = all_room_users[room_user]['person_id']
                reminders_enabled = all_room_users[room_user]['reminders_enabled']

        allow_dm = False
        if reminders_enabled == 'on':
            allow_dm = True

        session = requests.Session()
        #Get all the reviews for the pull request
        reviewers_data = self.get_approved_reviews(json_string, headers)
        approved_reviewers = reviewers_data['approved_reviewers']
        approved_reviews = reviewers_data['approved_reviews']

        required_approvals = self.dynamo.get_required_approvals(repo_name, room_id)

        pr_url = json_string['pull_request']['url']
        pr_search = session.get(pr_url, headers=headers)
        pr_json = pr_search.json()
        pr_is_mergeable = bool(pr_json['mergeable'])

        pull_request_title = json_string['pull_request']['title']
        pull_request_url = json_string['pull_request']['html_url']
        pull_request_hyperlink = f'<a href="{pull_request_url}">{pull_request_title}</a>'

        #checks-runs
        check_runs_url = f"https://api.github.com/repos/{repo_name}/commits/{branch_name}/check-runs"
        check_runs_json = session.get(check_runs_url, headers=headers).json()

        passed_check_runs = True
        skipped_checks = 0
        for run in check_runs_json['check_runs']:
            if run['conclusion'].lower() == 'skipped':
                skipped_checks += 1
            elif run['conclusion'].lower() != 'success':
                passed_check_runs = False

        skipped_checks_msg = ""
        if skipped_checks > 0:
            skipped_checks_msg = f" ({skipped_checks} skipped)"

        reviews_mark = self.EMOJIS['GREEN_CHECK']

        #default is red mark
        check_runs_mark = self.EMOJIS['RED_X']
        if passed_check_runs is True:
            check_runs_mark = self.EMOJIS['GREEN_CHECK']

        mergeable_mark = self.EMOJIS['RED_X']
        if pr_is_mergeable is True:
            mergeable_mark = self.EMOJIS['GREEN_CHECK']

        self.logging.debug("APPROVED: %d %d", approved_reviews, required_approvals)

        if approved_reviews >= required_approvals:
            message = (
                f"""Pull request {pull_request_hyperlink} has been approved in {repo_name} by {approved_reviewers}: {review_message}\n"""
                f"""- {reviews_mark} Has Required Approvals\n- {check_runs_mark} Passes CI Checks{skipped_checks_msg}"""
                f"""\n- {mergeable_mark} Is Mergeable"""
            )

            self.Api.messages.create(room_id, markdown=message)

            if allow_dm is True:
                self.logging.debug("Sending message to %s \n message = %s", pr_author, message)
                self.Api.messages.create(toPersonId=user_id, markdown=message)

    def delete_installation(self, installation_id):
        event_info = self.check_installation(installation_id)

        self.room_id = event_info[0]['room_id']

        self.table.delete_item(Key={'installation_id': str(installation_id)})
        self.table = self.dynamodb.Table(self.db_room_name)

        response = self.table.query(KeyConditionExpression=Key('room_id').eq(self.room_id))

        removed_repo_list = []

        for repo in response['Items'][0]['repos']:
            self.logging.debug("checking repo")
            if str(response['Items'][0]['repos'][repo]['installation_id']) == str(installation_id):
                removed_repo_list.append(repo)

                self.table.update_item(
                    Key={'room_id': self.room_id},
                    UpdateExpression="REMOVE #repo.#reponame",
                    ExpressionAttributeNames={
                        '#repo': 'repos',
                        '#reponame': repo
                    }
                )

        return removed_repo_list

    def check_installation(self, installation_id):
        self.dynamodb = boto3.resource('dynamodb')
        self.table = self.dynamodb.Table(self.db_installation_name)

        try:
            response = self.table.query(KeyConditionExpression=Key('installation_id').eq(str(installation_id)))
        except Exception:
            self.logging.debug('Cannot find record, quitting...')
            sys.exit(1)

        return response['Items']

    def get_approved_reviews(self, json_string, headers):
        """
        Obtains the number of approved reviews and those reviewers' name

        :param json_string: string, the body of the webhook
        :param headers: dictionary, header with token for git api

        :return: dictionary of the number of approved reviews and string of reviewers who approved the pr
        """

        session = requests.Session()
        pr_url = json_string['pull_request']['url']
        pr_reviews_url = pr_url + "/reviews"
        pr_reviews_search = session.get(pr_reviews_url, headers=headers)
        pr_reviews_json = pr_reviews_search.json()

        approved_reviewers = ""
        approved_reviews = 0

        for review in pr_reviews_json:
            if review['state'].lower() == 'approved':
                approved_reviews += 1
                approved_reviewers += review['user']['login'] + ", "
        approved_reviewers = approved_reviewers[:-2]

        return {'approved_reviews': approved_reviews, 'approved_reviewers': approved_reviewers}

    def get_code_owners_count(self, reviewers: tuple) -> int:
        """returns the number of code owners"""
        num_code_owners = 0

        for reviewer in reviewers:
            for _ in reviewer:
                num_code_owners += 1

        return num_code_owners
