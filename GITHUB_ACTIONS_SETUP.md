# GitHub Actions Setup Guide

This guide explains how to set up automated OCI instance provisioning using GitHub Actions with the new single-attempt mode.

## Overview

The GitHub Actions workflow provides an alternative to the continuous polling approach by:
- Running on a schedule (every 30 minutes by default)
- Making a single provisioning attempt per run
- Providing clear status reporting and notifications
- Automatically retrying on the next scheduled run for capacity issues

## Required Secrets

Configure these secrets in your GitHub repository settings (`Settings > Secrets and variables > Actions`):

### Required Secrets
- `OCI_CONFIG_CONTENT`: Content of your `~/.oci/config` file
- `OCI_PRIVATE_KEY`: Content of your OCI API private key file (usually `~/.oci/oci_api_key.pem`)

### Optional Notification Secrets
- `EMAIL`: Your email address (if using email notifications)
- `EMAIL_PASSWORD`: Your email app password (if using email notifications)
- `DISCORD_WEBHOOK`: Discord webhook URL for notifications
- `TELEGRAM_TOKEN`: Telegram bot token
- `TELEGRAM_USER_ID`: Your Telegram user ID

## Repository Variables

Configure these variables in your GitHub repository settings (`Settings > Secrets and variables > Actions > Variables`):

### Instance Configuration Variables
- `OCT_FREE_AD`: Availability Domain (default: "AD-1")
- `DISPLAY_NAME`: Instance display name (default: "github-actions-instance")
- `OCI_COMPUTE_SHAPE`: Instance shape (default: "VM.Standard.E2.1.Micro")
- `OCI_IMAGE_ID`: Specific image ID (optional, will auto-select if not provided)
- `OPERATING_SYSTEM`: OS name (default: "Canonical Ubuntu")
- `OS_VERSION`: OS version (default: "22.04")
- `ASSIGN_PUBLIC_IP`: Assign public IP (default: "false")
- `BOOT_VOLUME_SIZE`: Boot volume size in GB (default: "50")

### Notification Variables
- `NOTIFY_EMAIL`: Enable email notifications (default: "false")

## Setting Up Secrets

### 1. OCI Configuration

```bash
# Copy your OCI config content
cat ~/.oci/config
```
Paste this content as the `OCI_CONFIG_CONTENT` secret.

```bash
# Copy your OCI private key content
cat ~/.oci/oci_api_key.pem
```
Paste this content as the `OCI_PRIVATE_KEY` secret.

### 2. Discord Notifications (Optional)

1. Create a Discord webhook in your server
2. Copy the webhook URL and add it as the `DISCORD_WEBHOOK` secret

### 3. Email Notifications (Optional)

1. Generate an app password for your Gmail account
2. Add your email as the `EMAIL` secret
3. Add the app password as the `EMAIL_PASSWORD` secret
4. Set the `NOTIFY_EMAIL` variable to "true"

## Workflow Configuration

The workflow runs every 30 minutes by default. To modify the schedule, edit `.github/workflows/provision-instance.yml`:

```yaml
on:
  schedule:
    # Run every hour instead of every 30 minutes
    - cron: '0 * * * *'
```

### Schedule Examples
- Every 15 minutes: `'*/15 * * * *'`
- Every hour: `'0 * * * *'`
- Every 6 hours: `'0 */6 * * *'`
- Once daily at 9 AM UTC: `'0 9 * * *'`

## Manual Triggering

You can manually trigger the workflow:
1. Go to your repository's Actions tab
2. Select "OCI Instance Provisioning"
3. Click "Run workflow"

## Monitoring and Logs

### Job Summary
Each workflow run creates a summary showing:
- Provisioning status (success, capacity issue, fatal error)
- Detailed message
- Timestamp
- Next steps

### Artifacts
Logs are automatically uploaded as artifacts for 7 days:
- `provision_once.log`: Main provisioning log
- `setup_and_info.log`: Setup information
- `INSTANCE_CREATED`: Success marker file
- `ERROR_IN_CONFIG.log`: Configuration errors

### Exit Codes
- **0**: Success (instance created or already exists)
- **1**: Capacity issue (will retry automatically)
- **2**: Fatal error (requires manual intervention)

## Troubleshooting

### Common Issues

**"Fatal error - check configuration"**
- Verify all required secrets are set correctly
- Check that your OCI config and private key are valid
- Ensure the private key path in your OCI config matches the GitHub Actions setup

**"Capacity issue - will retry on next scheduled run"**
- This is normal for OCI Free Tier
- The workflow will automatically retry
- Consider adjusting the schedule frequency

**Workflow not running**
- Check that the repository has GitHub Actions enabled
- Verify the workflow file syntax is correct
- Ensure the cron schedule is valid

### Testing Configuration

Create a simple test workflow to verify your secrets:

```yaml
name: Test OCI Connection
on: workflow_dispatch

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v4
    - name: Test OCI Config
      run: |
        echo "${{ secrets.OCI_CONFIG_CONTENT }}" | head -5
        echo "Config length: $(echo "${{ secrets.OCI_CONFIG_CONTENT }}" | wc -c)"
```

## Security Considerations

- Never commit secrets to your repository
- Use repository secrets for sensitive data
- Consider using environment-specific repositories for production workloads
- Regularly rotate your OCI API keys
- Monitor your OCI usage through the console

## Integration with Local Polling

Both modes can coexist:
- Use local polling for development and testing
- Use GitHub Actions for automated, scheduled provisioning
- The same `oci.env` configuration works for both modes
- Set `MODE=SINGLE_ATTEMPT` for GitHub Actions, `MODE=POLLING` for local use